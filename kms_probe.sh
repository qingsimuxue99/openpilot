#!/bin/bash
# comma c3 投屏可行性诊断：下载带 kmsgrab 的 aarch64 ffmpeg，实测 kmsgrab 能否抓到 DRM 屏幕帧。
# 全程 timeout 包裹，绝不卡死。
set +e
echo "==== comma c3 投屏诊断 (kmsgrab) ===="
echo "ARCH=$(uname -m)"
echo "BRANCH=$(cat /data/params/d/GitBranch 2>/dev/null)"
echo "DISPLAY=[$DISPLAY]"
echo "QT_QPA_PLATFORM=[$QT_QPA_PLATFORM]"
echo "== UI 进程 =="
ps aux | grep -E "selfdrive/ui|ui/__main__|/ui " | grep -v grep || echo "(未匹配到 UI 进程)"
echo "== 显示服务器 =="
ps aux | grep -E "Xorg|weston|wayland|gnome-shell" | grep -v grep || echo "(无 X/Wayland 进程 -> 确认走 DRM/EGL)"
echo "== 当前 ffmpeg 是否带 kmsgrab =="
ffmpeg -hide_banner -devices 2>&1 | grep -i kmsgrab || echo "NO kmsgrab in /usr/local/bin/ffmpeg"
echo "== /dev/dri =="
ls -l /dev/dri/ 2>/dev/null
echo "== 下载带 kmsgrab 的 aarch64 ffmpeg (BtbN, 超时200s) =="
timeout 200 curl -sL "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linuxarm64-gpl.tar.xz" -o /tmp/ff.txz && echo "下载完成" || echo "下载失败(无外网/超时)"
if [ -s /tmp/ff.txz ]; then
  rm -rf /tmp/ffb && mkdir -p /tmp/ffb && tar -xf /tmp/ff.txz -C /tmp/ffb 2>/dev/null
  FF=$(find /tmp/ffb -name ffmpeg -type f | head -1)
  if [ -n "$FF" ]; then
    chmod +x "$FF"
    echo "FF=$FF"
    echo "== 该 ffmpeg 是否带 kmsgrab =="
    "$FF" -hide_banner -devices 2>&1 | grep -i kmsgrab || echo "该版本仍无 kmsgrab"
    echo "== kmsgrab 实测(8s 超时) =="
    timeout 8 "$FF" -hide_banner -loglevel error -f kmsgrab -i /dev/dri/card0 -frames:v 1 -f image2 -c:v mjpeg /tmp/kms_test.jpg 2>&1 | head -25
    if [ -s /tmp/kms_test.jpg ]; then
      echo ">>> kmsgrab 成功! 帧大小=$(stat -c%s /tmp/kms_test.jpg) 字节 头=$(head -c4 /tmp/kms_test.jpg | xxd -p)"
    else
      echo ">>> kmsgrab 失败(可能: 该版本无 kmsgrab / DRM 主设备被 UI 占用 / 需指定 CRTC 或平面)"
    fi
  else
    echo "未找到 ffmpeg 二进制(解包失败?)"
  fi
else
  echo "未下载到 ffmpeg，跳过实测"
fi
echo "==== DONE ===="
