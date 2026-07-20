#!/bin/bash
# comma c3 投屏: 本地编译 libdrm 抓屏工具, 抓一帧 scanout framebuffer 并转 JPEG
# 改进: 枚举全部 plane, 优先选 PRIMARY 平面(合成后的 UI 屏幕); GetFB2 失败回退 legacy GetFB
# 关键产出: 平面类型 / 分辨率 / fourcc / modifier(是否 tiled) —— 决定 1.0.30 方案
set -u
SRC=/tmp/drmgrab.c
BIN=/tmp/drmgrab
RAW=/tmp/screen.raw
JPG=/tmp/screen_drm.jpg
DEV=/dev/dri/card0
FFMPEG=$(command -v ffmpeg || echo /usr/local/bin/ffmpeg)

echo "==== comma c3 drmgrab: 编译 + 抓 PRIMARY 平面帧 + 转JPEG ===="
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
static uint64_t plane_type(int fd, uint32_t pid){
  drmModeObjectProperties*props=drmModeObjectGetProperties(fd,pid,DRM_MODE_OBJECT_PLANE);
  uint64_t t=0xff;
  if(props){
    for(uint32_t i=0;i<props->count_props;i++){
      drmModePropertyRes*p=drmModeGetProperty(fd,props->props[i]);
      if(p){ if(!strcmp(p->name,"type")) t=props->prop_values[i]; drmModeFreeProperty(p); }
    }
    drmModeFreeObjectProperties(props);
  }
  return t;
}
/* 抓取单个 fb: 优先 GetFB2(含 modifier), 失败回退 legacy GetFB。返回 0=成功 */
int grab(int fd, uint32_t fbid, const char*out, int verbose){
  drmModeFB2*fb=drmModeGetFB2(fd,fbid);
  uint32_t w=0,h=0,pitch=0,bpp=32,handle=0;
  char fs[8]="?"; uint64_t mod=0; int mode=-1;
  if(fb){
    mode=0; fourcc_str(fb->pixel_format,fs);
    w=fb->width;h=fb->height;pitch=fb->pitches[0];handle=fb->handles[0];mod=fb->modifier;
    if(verbose)printf("  [FB2] ok %ux%u fourcc=%s mod=0x%llx pitch0=%u h0=%u\n",w,h,fs,(unsigned long long)mod,pitch,handle);
    if(!handle){drmModeFreeFB2(fb);mode=-1;}
  }
  if(mode<0){
    drmModeFB*fb0=drmModeGetFB(fd,fbid);
    if(fb0){
      mode=1; w=fb0->width;h=fb0->height;pitch=fb0->pitch;bpp=fb0->bpp;handle=fb0->handle;
      if(bpp==32)strcpy(fs,"XR24"); else if(bpp==16)strcpy(fs,"RGBP"); else if(bpp==24)strcpy(fs,"RGB3"); else sprintf(fs,"B%u",bpp);
      if(verbose)printf("  [FB-legacy] ok %ux%u pitch=%u bpp=%u depth=%u handle=%u\n",w,h,pitch,bpp,fb0->depth,handle);
      if(!handle){drmModeFreeFB(fb0);mode=-1;}
    }
  }
  if(mode<0){ if(verbose)printf("  -> grab 失败(无 handle)\n"); return -1; }
  int pfd=-1;
  if(drmPrimeHandleToFD(fd,handle,O_RDONLY,&pfd)||pfd<0){ if(verbose)printf("  PRIME_FAIL\n"); return -1; }
  size_t sz=(size_t)pitch*h;
  if(sz> 200*1024*1024){ if(verbose)printf("  too big %zu\n",sz); return -1; }
  void*m=mmap(NULL,sz,PROT_READ,MAP_SHARED,pfd,0);
  if(m==MAP_FAILED){perror("mmap");return -1;}
  FILE*fo=fopen(out,"wb"); if(!fo){perror("fopen");return -1;}
  fwrite(m,1,sz,fo);fclose(fo);
  unsigned char*pc=(unsigned char*)m;
  printf("WROTE %s bytes=%zu\n",out,sz);
  printf("PREVIEW %02x %02x %02x %02x %02x %02x %02x %02x\n",pc[0],pc[1],pc[2],pc[3],pc[4],pc[5],pc[6],pc[7]);
  printf("META W=%u H=%u PITCH=%u FOURCC=%s MOD=0x%llx PATH=%d\n",w,h,pitch,fs,(unsigned long long)mod,mode);
  return 0;
}
int main(int argc,char**argv){
  const char*dev=argc>1?argv[1]:"/dev/dri/card0";
  const char*out=argc>2?argv[2]:"/tmp/screen.raw";
  int fd=open(dev,O_RDWR|O_CLOEXEC);
  if(fd<0){perror("open card");return 2;}
  drmSetClientCap(fd,DRM_CLIENT_CAP_UNIVERSAL_PLANES,1);
  drmSetClientCap(fd,DRM_CLIENT_CAP_ATOMIC,1);
  uint32_t cands[64]; uint64_t ctypes[64]; int nc=0;
  drmModeRes*res=drmModeGetResources(fd);
  if(res){
    for(int i=0;i<res->count_crtcs;i++){
      drmModeCrtc*c=drmModeGetCrtc(fd,res->crtcs[i]);
      if(c){ if(c->buffer_id){ if(nc<64){cands[nc]=c->buffer_id;ctypes[nc]=1;} nc++; printf("CRTC %u fb=%u %dx%d\n",res->crtcs[i],c->buffer_id,c->width,c->height);} drmModeFreeCrtc(c);}
    }
  }
  drmModePlaneRes*pr=drmModeGetPlaneResources(fd);
  if(pr){
    for(uint32_t i=0;i<pr->count_planes;i++){
      drmModePlane*pl=drmModeGetPlane(fd,pr->planes[i]);
      if(!pl)continue;
      uint64_t t=plane_type(fd,pr->planes[i]);
      const char*tn = t==1?"PRIMARY":(t==0?"OVERLAY":(t==2?"CURSOR":"?"));
      printf("PLANE %u type=%s fb=%u crtc=%u\n",pr->planes[i],tn,pl->fb_id,pl->crtc_id);
      if(pl->fb_id && nc<64){ cands[nc]=pl->fb_id; ctypes[nc]=t; nc++; }
      drmModeFreePlane(pl);
    }
    drmModeFreePlaneResources(pr);
  }
  if(nc==0){fprintf(stderr,"NO_FB\n");return 4;}
  /* 第一轮: 仅 PRIMARY(type==1); 第二轮: 其余任意(overlay/cursor/crtc) */
  for(int pass=0;pass<2;pass++){
    for(int i=0;i<nc;i++){
      if(pass==0 && ctypes[i]!=1) continue;
      if(pass==1 && ctypes[i]==1) continue;
      const char*tn = ctypes[i]==1?"PRIMARY":(ctypes[i]==0?"OVERLAY":"?");
      printf("TRY fb=%u (%s)\n", cands[i], tn);
      if(grab(fd,cands[i],out,1)==0) return 0;
    }
  }
  fprintf(stderr,"ALL_GRAB_FAILED\n");
  return 5;
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
  echo "✗ 抓帧失败 rc=$RC"
  echo "   若仍 ALL_GRAB_FAILED / EINVAL → 说明 GetFB2 被内核限制(非 master), 转 kmsgrab/apt 路线"
  exit 1
fi

# 解析元数据
MLINE=$(grep '^META' /tmp/drm_run.log | head -1)
W=$(echo "$MLINE" | sed -n 's/.*W=\([0-9]*\).*/\1/p')
H=$(echo "$MLINE" | sed -n 's/.*H=\([0-9]*\).*/\1/p')
PITCH=$(echo "$MLINE" | sed -n 's/.*PITCH=\([0-9]*\).*/\1/p')
FOURCC=$(echo "$MLINE" | sed -n 's/.*FOURCC=\([A-Za-z0-9]*\).*/\1/p')
MOD=$(echo "$MLINE" | sed -n 's/.*MOD=\(0x[0-9a-fA-F]*\).*/\1/p')
PATHM=$(echo "$MLINE" | sed -n 's/.*PATH=\([0-9]*\).*/\1/p')
echo "[3] 元数据: W=$W H=$H PITCH=$PITCH FOURCC=$FOURCC MOD=$MOD PATH=$PATHM"

# 像素格式 / 每像素字节
BPP=4
PIXFMT=bgr0
case "$FOURCC" in
  AR24) PIXFMT=bgra; BPP=4;;
  XR24) PIXFMT=bgr0; BPP=4;;
  AB24) PIXFMT=rgba; BPP=4;;
  XB24) PIXFMT=rgb0; BPP=4;;
  RGBP|RGB565) PIXFMT=rgb565le; BPP=2;;
esac
SW=$((PITCH/BPP))
echo "[4] 转 JPEG (pix_fmt=$PIXFMT stride_w=$SW crop=${W}x${H})"
if [ -z "$W" ] || [ -z "$PITCH" ]; then echo "✗ 元数据解析失败"; exit 1; fi
"$FFMPEG" -hide_banner -loglevel error -f rawvideo -pix_fmt "$PIXFMT" -s "${SW}x${H}" -i "$RAW" \
  -vf "crop=${W}:${H}:0:0" -frames:v 1 -q:v 3 "$JPG" 2>/tmp/drm_ff.log
if [ -s "$JPG" ]; then
  echo "✓✓ 生成 $JPG  大小=$(stat -c%s "$JPG")B 头=$(head -c3 "$JPG"|xxd -p)"
  echo "   (ffd8ff 开头即有效 JPEG)"
  if [ "$MOD" != "0x0" ] && [ -n "$MOD" ]; then
    echo "   ⚠ MODIFIER 非线性($MOD): framebuffer 可能为 tiled/压缩(UBWC), 直读或花屏; 但仍请先用手机拍此图看是否为正常 UI"
  else
    echo "   ✓ MODIFIER=0x0(线性): 直读即可得正确像素, 1.0.30 直接可用"
  fi
  echo ">>> 请把上面 [3] 元数据 + 这张图(/tmp/screen_drm.jpg)发我。可在设备用 scp 取出, 或微信发我看是否含 UI"
else
  echo "✗ 转码失败:"; cat /tmp/drm_ff.log
fi
echo "==== DONE ===="
