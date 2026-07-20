#!/bin/bash
# comma c3 投屏: 本地编译 libdrm 抓屏工具, 抓一帧 scanout framebuffer 并转 JPEG
# 关键产出: 分辨率 / fourcc / modifier(是否 tiled) —— 决定 1.0.30 方案
set -u
SRC=/tmp/drmgrab.c
BIN=/tmp/drmgrab
RAW=/tmp/screen.raw
JPG=/tmp/screen_drm.jpg
DEV=/dev/dri/card0
FFMPEG=$(command -v ffmpeg || echo /usr/local/bin/ffmpeg)

echo "==== comma c3 drmgrab: 编译 + 抓帧 + 转JPEG ===="
echo "[env] uid=$(id -u)  ffmpeg=$FFMPEG  libdrm=$(pkg-config --modversion libdrm 2>/dev/null)"

cat > "$SRC" <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
static void fourcc_str(uint32_t f,char*s){s[0]=f&0xff;s[1]=(f>>8)&0xff;s[2]=(f>>16)&0xff;s[3]=(f>>24)&0xff;s[4]=0;}
int main(int argc,char**argv){
  const char*dev=argc>1?argv[1]:"/dev/dri/card0";
  const char*out=argc>2?argv[2]:"/tmp/screen.raw";
  int fd=open(dev,O_RDWR|O_CLOEXEC);
  if(fd<0){perror("open card");return 2;}
  drmSetClientCap(fd,DRM_CLIENT_CAP_UNIVERSAL_PLANES,1);
  drmSetClientCap(fd,DRM_CLIENT_CAP_ATOMIC,1);
  uint32_t fbid=0;
  drmModeRes*res=drmModeGetResources(fd);
  if(res){
    for(int i=0;i<res->count_crtcs&&!fbid;i++){
      drmModeCrtc*c=drmModeGetCrtc(fd,res->crtcs[i]);
      if(c){if(c->buffer_id){fbid=c->buffer_id;printf("CRTC %u active fb=%u %dx%d\n",res->crtcs[i],c->buffer_id,c->width,c->height);}drmModeFreeCrtc(c);}
    }
  }
  if(!fbid){
    drmModePlaneRes*pr=drmModeGetPlaneResources(fd);
    if(pr){for(uint32_t i=0;i<pr->count_planes&&!fbid;i++){drmModePlane*pl=drmModeGetPlane(fd,pr->planes[i]);if(pl){if(pl->fb_id){fbid=pl->fb_id;printf("PLANE %u fb=%u\n",pr->planes[i],pl->fb_id);}drmModeFreePlane(pl);}}drmModeFreePlaneResources(pr);}
  }
  if(!fbid){fprintf(stderr,"NO_ACTIVE_FB\n");return 4;}
  drmModeFB2*fb=drmModeGetFB2(fd,fbid);
  if(!fb){perror("getfb2(need root?)");return 5;}
  char fs[8];fourcc_str(fb->pixel_format,fs);
  printf("FB id=%u %ux%u fourcc=%s modifier=0x%llx pitch0=%u off0=%u h0=%u pitch1=%u off1=%u h1=%u\n",
    fbid,fb->width,fb->height,fs,(unsigned long long)fb->modifier,
    fb->pitches[0],fb->offsets[0],fb->handles[0],fb->pitches[1],fb->offsets[1],fb->handles[1]);
  if(!fb->handles[0]){fprintf(stderr,"NO_HANDLE(need root/master)\n");return 6;}
  int pfd=-1;
  if(drmPrimeHandleToFD(fd,fb->handles[0],O_RDONLY,&pfd)||pfd<0){fprintf(stderr,"PRIME_FAIL\n");return 7;}
  size_t sz=(size_t)fb->pitches[0]*fb->height;
  if(fb->handles[1]&&fb->offsets[1]>0){size_t s2=fb->offsets[1]+(size_t)fb->pitches[1]*fb->height;if(s2>sz)sz=s2;}
  void*m=mmap(NULL,sz,PROT_READ,MAP_SHARED,pfd,0);
  if(m==MAP_FAILED){perror("mmap");return 8;}
  FILE*fo=fopen(out,"wb");if(!fo){perror("fopen");return 9;}
  fwrite(m,1,sz,fo);fclose(fo);
  printf("WROTE %s bytes=%zu\n",out,sz);
  printf("META W=%u H=%u PITCH=%u FOURCC=%s MOD=0x%llx\n",fb->width,fb->height,fb->pitches[0],fs,(unsigned long long)fb->modifier);
  return 0;
}
EOF

echo "[1] 编译"
gcc -O2 -o "$BIN" "$SRC" $(pkg-config --cflags --libs libdrm) 2>/tmp/drm_cc.log
if [ ! -x "$BIN" ]; then echo "✗ 编译失败:"; cat /tmp/drm_cc.log; exit 1; fi
echo "✓ 编译成功 $BIN"

echo "[2] 抓帧 (sudo, prime handle 需 root)"
rm -f "$RAW"
sudo "$BIN" "$DEV" "$RAW" >/tmp/drm_run.log 2>&1
RC=$?
cat /tmp/drm_run.log
if [ $RC -ne 0 ] || [ ! -s "$RAW" ]; then
  echo "✗ 抓帧失败 rc=$RC —— 错误码含义:"
  echo "   5=getfb2失败(需root) 6=无handle 7=prime失败 8=mmap失败(可能tiled不可直读)"
  exit 1
fi

# 解析元数据 (从上一次运行的 log, 不重复抓)
MLINE=$(grep '^META' /tmp/drm_run.log | head -1)
W=$(echo "$MLINE" | sed -n 's/.*W=\([0-9]*\).*/\1/p')
H=$(echo "$MLINE" | sed -n 's/.*H=\([0-9]*\).*/\1/p')
PITCH=$(echo "$MLINE" | sed -n 's/.*PITCH=\([0-9]*\).*/\1/p')
FOURCC=$(echo "$MLINE" | sed -n 's/.*FOURCC=\([A-Za-z0-9]*\).*/\1/p')
MOD=$(echo "$MLINE" | sed -n 's/.*MOD=\(0x[0-9a-fA-F]*\).*/\1/p')
echo "[3] 元数据: W=$W H=$H PITCH=$PITCH FOURCC=$FOURCC MOD=$MOD"

# 判断 modifier
if [ "$MOD" != "0x0" ] && [ -n "$MOD" ]; then
  echo "⚠ MODIFIER 非线性 ($MOD) —— framebuffer 是 tiled/压缩(UBWC), 直读会花屏。"
  echo "  仍尝试转一帧供肉眼判断; 若花屏, 1.0.30 需走 GPU detile 或改抓 plane。"
fi

echo "[4] 转 JPEG (按 pitch 推算 stride 宽度再裁剪)"
if [ -z "$W" ] || [ -z "$PITCH" ]; then echo "✗ 元数据解析失败, 跳过转码"; exit 1; fi
SW=$((PITCH/4))
# XR24/AR24 => bgra 顺序; XB24/AB24 => rgba. comma DPU 常见 XR24(=XRGB8888,小端存 BGRX)
PIXFMT=bgr0
case "$FOURCC" in
  AR24) PIXFMT=bgra;; XR24) PIXFMT=bgr0;;
  AB24) PIXFMT=rgba;; XB24) PIXFMT=rgb0;;
esac
echo "   使用 pix_fmt=$PIXFMT stride_w=$SW crop=${W}x${H}"
"$FFMPEG" -hide_banner -loglevel error -f rawvideo -pix_fmt "$PIXFMT" -s "${SW}x${H}" -i "$RAW" \
  -vf "crop=${W}:${H}:0:0" -frames:v 1 -q:v 3 "$JPG" 2>/tmp/drm_ff.log
if [ -s "$JPG" ]; then
  echo "✓✓ 生成 $JPG  大小=$(stat -c%s "$JPG")B 头=$(head -c3 "$JPG"|xxd -p)"
  echo "   (ffd8ff 开头即有效 JPEG)"
  echo ">>> 请把上面 [3] 元数据行 + 这个结果发我。若能看图, 可通过工具箱旧接口临时验证。"
else
  echo "✗ 转码失败:"; cat /tmp/drm_ff.log
fi
echo "==== DONE ===="
