#!/usr/bin/env bash
# comma c3 投屏: 获取带 kmsgrab 的 ffmpeg(apt 优先修源, 静态二进制兜底) + 实测抓帧
set -uo pipefail
echo "==== comma c3: 获取 kmsgrab ffmpeg(apt修源优先, 静态兜底) ===="

SUDO=""
if [ "$(id -u)" -eq 0 ]; then SUDO=""; echo "[权限] root"; 
elif sudo -n true 2>/dev/null; then SUDO="sudo"; echo "[权限] sudo 免密";
else echo "[权限] 无 root, 静态路线受限"; fi

FF=""
KR=""

# ---------- [A] apt 优先: 补 main/universe 后装 ffmpeg 6.x(自带 kmsgrab) ----------
if [ -n "$SUDO" ]; then
  echo "[A] apt 路线: 检查/补充 noble main 组件"
  CODENAME=$(. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-noble}")
  echo "  codename=$CODENAME"
  if ! grep -rq "main" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null; then
    echo "  -> 缺 main, 写入 /etc/apt/sources.list.d/c3-main.list"
    echo "deb http://ports.ubuntu.com/ubuntu-ports $CODENAME main universe" | $SUDO tee /etc/apt/sources.list.d/c3-main.list >/dev/null
  else
    echo "  main 已存在, 跳过写入"
  fi
  $SUDO apt-get update 2>&1 | tail -2
  echo "  apt-cache policy libavfilter9 (候选应 >= 7:6.0):"
  $SUDO apt-cache policy libavfilter9 2>/dev/null | sed -n '1,6p'
  echo "  安装 ffmpeg..."
  $SUDO apt-get install -y ffmpeg 2>&1 | tail -8
fi

# ---------- [B] 静态 ffmpeg(arm64) 兜底: 修正后的子包 URL ----------
echo "[B] 静态 ffmpeg(arm64) 候选探测(头部请求, 不整下)"
URLS=(
 "https://cdn.jsdelivr.net/npm/ffmpeg-static-linux-arm64@5.2.0/ffmpeg"
 "https://cdn.jsdelivr.net/npm/ffmpeg-static-linux-arm64/ffmpeg"
 "https://cdn.jsdelivr.net/npm/@ffmpeg-installer/linux-arm64@1.1.0/ffmpeg"
 "https://cdn.jsdelivr.net/npm/@ffmpeg-installer/linux-arm64/ffmpeg"
)
for u in "${URLS[@]}"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 -r 0-1023 "$u" 2>/dev/null)
  echo "  $code  $u"
done
TMPF=/tmp/ffmpeg_static
rm -f "$TMPF"
for u in "${URLS[@]}"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 -r 0-1023 "$u" 2>/dev/null)
  if [ "$code" = "200" ]; then
    echo "  下载: $u"
    if curl -fsSL --max-time 400 "$u" -o "$TMPF" 2>/dev/null && file "$TMPF" 2>/dev/null | grep -qi ELF; then
      chmod +x "$TMPF"; echo "  OK ELF $(stat -c%s "$TMPF") bytes"; FF="$TMPF"; break
    else echo "  XX 失败"; rm -f "$TMPF"; fi
  fi
done

# ---------- [C] 在可用 ffmpeg 中挑一个带 kmsgrab 的 ----------
echo "[C] 挑选带 kmsgrab 的 ffmpeg"
for cand in "$FF" /usr/bin/ffmpeg /usr/local/bin/ffmpeg $(command -v ffmpeg 2>/dev/null); do
  [ -x "$cand" ] || continue
  if "$cand" -hide_banner -devices 2>&1 | grep -qi kmsgrab; then FF="$cand"; KR=1; echo "  kmsgrab@ $FF"; break; fi
done
if [ -z "$KR" ]; then
  echo "  XX 没有带 kmsgrab 的 ffmpeg (候选均不支持)"
  for c in "$FF" /usr/bin/ffmpeg /usr/local/bin/ffmpeg; do [ -x "$c" ] && echo "    $c -> $($c -version 2>/dev/null|head -1)"; done
  echo "==== DONE (失败) ===="; exit 1
fi

# ---------- [D] kmsgrab 抓帧实测(先当前用户, 失败再 sudo) ----------
echo "[D] kmsgrab 抓一帧 -> /tmp/kms_test.png"
rm -f /tmp/kms_test.png
timeout 40 "$FF" -hide_banner -loglevel info -f kmsgrab -i /dev/dri/card0 -frames:v 1 -y /tmp/kms_test.png 2>&1 | tail -20
if [ ! -s /tmp/kms_test.png ] && [ -n "$SUDO" ]; then
  echo "  当前用户失败, 试 sudo..."
  $SUDO timeout 40 "$FF" -hide_banner -loglevel info -f kmsgrab -i /dev/dri/card0 -frames:v 1 -y /tmp/kms_test.png 2>&1 | tail -20
fi
echo "---"
if [ -s /tmp/kms_test.png ]; then
  echo "OK 抓帧成功 $(stat -c%s /tmp/kms_test.png) bytes"; file /tmp/kms_test.png
  echo "  => 可用命令: $FF -f kmsgrab -i /dev/dri/card0 -f image2pipe -c:v mjpeg -q:v 4 -r 15 -"
else
  echo "XX 抓帧失败(见上错误)"
fi
echo "==== DONE ===="
