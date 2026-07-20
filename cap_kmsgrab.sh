#!/bin/bash
# comma c3 投屏决定性尝试：apt 装 Debian ffmpeg(带 kmsgrab) + 实测 kmsgrab 抓 /dev/dri/card0
# 设计：零 GitHub 大文件依赖(走 Debian 镜像)；全程 timeout 防卡死；成功/失败都打印明确结论。
set -u
echo "==== comma c3 投屏: apt 装 ffmpeg(kmsgrab) + 实测 ===="
KFF=/usr/bin/ffmpeg
OLDFF=/usr/local/bin/ffmpeg

echo "[1] 现有 ffmpeg 能力"
if [ -x "$OLDFF" ]; then
  echo "  $OLDFF 支持的设备:"
  "$OLDFF" -hide_banner -devices 2>/dev/null | grep -iE 'kmsgrab|x11grab|waylandgrab|fbdev' || echo "    (无 kmsgrab/x11grab/waylandgrab；有 fbdev 但本设备无 /dev/fb0)"
else
  echo "  $OLDFF 不存在"
fi

echo "[2] apt 连通性 (Debian 镜像, 超时60s)"
if timeout 60 apt-get update >/tmp/apt_up.log 2>&1; then
  echo "  apt update OK"
else
  echo "  apt update 失败/超时 —— 尾部:"
  tail -4 /tmp/apt_up.log
fi

echo "[3] 安装 Debian ffmpeg (带 kmsgrab, arm64 原生)"
if [ -x "$KFF" ] && "$KFF" -hide_banner -devices 2>/dev/null | grep -qi kmsgrab; then
  echo "  $KFF 已带 kmsgrab, 跳过安装"
else
  echo "  执行 apt-get install -y ffmpeg (超时420s)..."
  if timeout 420 apt-get install -y ffmpeg >/tmp/apt_ff.log 2>&1; then
    echo "  安装完成"
  else
    echo "  安装失败, 尾部:"
    tail -10 /tmp/apt_ff.log
  fi
fi

echo "[4] 验证 $KFF kmsgrab"
if [ -x "$KFF" ] && "$KFF" -hide_banner -devices 2>/dev/null | grep -qi kmsgrab; then
  echo "  OK $KFF 带 kmsgrab"
else
  echo "  XX $KFF 仍无 kmsgrab, 无法走此路"
  echo "==== DONE ===="
  exit 0
fi

echo "[5] 实测 kmsgrab 抓一帧 (优先 sudo, 失败回退普通用户)"
rm -f /tmp/kms.jpg /tmp/kms_sudo.log /tmp/kms_user.log

# 5a 普通用户
timeout 15 "$KFF" -hide_banner -loglevel info -f kmsgrab -i /dev/dri/card0 -frames:v 1 -f image2 -c:v mjpeg -q:v 3 /tmp/kms_user.jpg >/tmp/kms_user.log 2>&1
RCU=$?
# 5b sudo
sudo timeout 15 "$KFF" -hide_banner -loglevel info -f kmsgrab -i /dev/dri/card0 -frames:v 1 -f image2 -c:v mjpeg -q:v 3 /tmp/kms_sudo.jpg >/tmp/kms_sudo.log 2>&1
RCS=$?

for TAG in user sudo; do
  JF=/tmp/kms_$TAG.jpg
  LG=/tmp/kms_$TAG.log
  RC=$([ "$TAG" = user ] && echo $RCU || echo $RCS)
  HEAD=$(head -c2 "$JF" 2>/dev/null | xxd -p 2>/dev/null)
  if [ -s "$JF" ] && [ "$HEAD" = "ffd8" ]; then
    SZ=$(stat -c%s "$JF")
    DIM=$(grep -oE 'Stream #0:0: Video: mjpeg[^,]*[0-9]+x[0-9]+' "$LG" | grep -oE '[0-9]+x[0-9]+' | head -1)
    echo "  >>> [$TAG] kmsgrab 成功! 大小=$SZ 头=ffd8 分辨率=${DIM:-未知}"
    echo "  >>> 请把 /tmp/kms_$TAG.jpg 发我确认是 UI 画面"
    echo "  >>> 可用命令: sudo $KFF -f kmsgrab -i /dev/dri/card0 -f image2pipe -c:v mjpeg -q:v 3 -r 15 -"
    echo "KMGRAB_OK=1" > /tmp/kmsgrab_works
    echo "KMGRAB_CMD=sudo $KFF -f kmsgrab -i /dev/dri/card0 -f image2pipe -c:v mjpeg -q:v 3 -r 15 -" > /tmp/kmsgrab_cmd.txt
  else
    echo "  >>> [$TAG] kmsgrab 失败 rc=$RC, 尾部:"
    tail -6 "$LG"
  fi
done

echo "==== DONE ===="
