#!/bin/bash
echo "==== comma c3 抓屏深探 cap_probe2 ===="
echo
echo "[1] 全系统搜索 weston 截图组件"
find / -xdev \( -name 'weston-screenshooter' -o -name 'screenshooter*.so' \) 2>/dev/null
echo
echo "[2] /usr/bin/weston* 与 /usr/lib/weston/"
ls -la /usr/bin/weston* 2>/dev/null
ls -la /usr/lib/weston/ 2>/dev/null
echo
echo "[3] weston.ini 的 modules 配置 (/usr/comma/weston.ini)"
if [ -f /usr/comma/weston.ini ]; then
  grep -ni -A6 'module\|screenshoot' /usr/comma/weston.ini 2>/dev/null || echo "(weston.ini 无 module/screenshoot 配置)"
else
  echo "无 /usr/comma/weston.ini"
fi
echo
echo "[4] weston 启动方式"
echo "-- 进程 --"
ps aux | grep -i '[w]eston'
echo "-- systemd 服务 --"
ls -la /etc/systemd/system/weston* /lib/systemd/system/weston* /usr/lib/systemd/system/weston* 2>/dev/null || echo "(无 weston systemd 服务)"
echo "-- openpilot launch 中的 weston 启动 --"
grep -rn 'weston' /data/openpilot/launch* /data/openpilot/selfdrive/manager/process.py 2>/dev/null | head
echo
echo "[5] /usr/bin/ffmpeg 是否存在及能力"
if [ -x /usr/bin/ffmpeg ]; then
  echo "存在, 支持的抓取设备:"; /usr/bin/ffmpeg -hide_banner -devices 2>&1 | grep -iE 'kmsgrab|wayland|xcb|fbdev|v4l'
  /usr/bin/ffmpeg -hide_banner -version 2>&1 | head -1
else
  echo "无 /usr/bin/ffmpeg (只有 /usr/local/bin 那个无 kmsgrab)"
fi
echo
echo "[6] apt 源是否可装 ffmpeg (Debian 版通常带 kmsgrab)"
if command -v apt-get >/dev/null; then
  apt-cache policy ffmpeg 2>/dev/null | head -8 || echo "(apt-cache 失败)"
else
  echo "无 apt-get"
fi
echo
echo "[7] 编译工具链"
command -v gcc cc make pkg-config 2>/dev/null || echo "无编译工具"
echo
echo "[8] openpilot 自带截图工具"
ls -la /data/openpilot/tools/lib/weston.py 2>/dev/null
find /data/openpilot -xdev -iname '*screenshot*' 2>/dev/null | head
echo
echo "[9] /dev/dri 权限 (kmsgrab 可走 render node 绕过 master)"
ls -la /dev/dri/
echo
echo "==== DONE ===="
