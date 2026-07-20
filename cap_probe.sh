#!/bin/bash
# comma c3 投屏抓取方式探测：设备跑 Weston(Wayland)，UI 为 wayland-egl 客户端。
# 不下载任何东西，秒级出结果，全程 timeout 防卡死。
set +e
echo "==== comma c3 投屏抓取探测 (Wayland/Weston) ===="
echo "DISPLAY=[$DISPLAY]  QT_QPA_PLATFORM=[$QT_QPA_PLATFORM]"
echo "== ffmpeg 全部输入设备 =="
ffmpeg -hide_banner -devices 2>&1 | sed -n '/Devices:/,/^$/p'
echo "== 截图/抓屏工具位置 =="
for t in weston-screenshooter grim wlr-randr wayland-info; do
  p=$(command -v $t 2>/dev/null); echo "$t -> ${p:-未找到}"
done
echo "== 尝试 weston-screenshooter 抓一帧到 /tmp/ws.png =="
if command -v weston-screenshooter >/dev/null; then
  timeout 12 weston-screenshooter /tmp/ws.png 2>&1 | head -12
  if [ -s /tmp/ws.png ]; then echo ">>> weston-screenshooter 成功 大小=$(stat -c%s /tmp/ws.png)字节 头=$(head -c4 /tmp/ws.png | xxd -p)"; else echo ">>> weston-screenshooter 失败(可能 compositor 未启用 screenshooter 模块)"; fi
else
  echo "无 weston-screenshooter"
fi
echo "== 尝试 ffmpeg waylandgrab =="
if ffmpeg -hide_banner -devices 2>&1 | grep -qi waylandgrab; then
  timeout 12 ffmpeg -hide_banner -loglevel error -f waylandgrab -i :0.0 -frames:v 1 -f image2 -c:v mjpeg /tmp/wl.jpg 2>&1 | head -12
  if [ -s /tmp/wl.jpg ]; then echo ">>> waylandgrab 成功 大小=$(stat -c%s /tmp/wl.jpg)字节"; else echo ">>> waylandgrab 失败"; fi
else
  echo "ffmpeg 无 waylandgrab"
fi
echo "== 尝试 ffmpeg xcbgrab (若有 X 兼用) =="
if ffmpeg -hide_banner -devices 2>&1 | grep -qi xcbgrab; then
  timeout 12 ffmpeg -hide_banner -loglevel error -f xcbgrab -i :0.0 -frames:v 1 -f image2 -c:v mjpeg /tmp/xcb.jpg 2>&1 | head -12
  if [ -s /tmp/xcb.jpg ]; then echo ">>> xcbgrab 成功"; else echo ">>> xcbgrab 失败"; fi
else
  echo "ffmpeg 无 xcbgrab"
fi
echo "==== DONE ===="
