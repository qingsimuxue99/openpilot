#!/usr/bin/env bash
# comma c3 投屏探测: 获取带 kmsgrab 的静态 ffmpeg(arm64) 并实测抓帧
# 路线: 优先从 jsdelivr(npm) 下载静态 ffmpeg(免 apt/免 root 下载), 失败且有 root 时再试 apt
set -uo pipefail

FF=/tmp/ffmpeg_kms
echo "==== comma c3 投屏: 获取 kmsgrab ffmpeg 并实测 ===="

# [0] 权限检测
echo "[0] 权限检测"
echo "  uid=$(id -u) user=$(id -un)"
SUDO=""
if [ "$(id -u)" -eq 0 ]; then SUDO=""; echo "  当前是 root"; 
elif sudo -n true 2>/dev/null; then SUDO="sudo"; echo "  sudo 免密可用";
else echo "  非 root, sudo 不可用(部分路径受限)"; fi

# [1] 尝试从 jsdelivr/npm 下载静态 ffmpeg(arm64, 通常含 kmsgrab)
echo "[1] 从 jsdelivr(npm) 下载静态 ffmpeg(arm64)"
URLS=(
 "https://cdn.jsdelivr.net/npm/ffmpeg-static@5.2.0/ffmpeg-linux-arm64"
 "https://cdn.jsdelivr.net/npm/ffmpeg-static@5.1.0/ffmpeg-linux-arm64"
 "https://cdn.jsdelivr.net/npm/@ffmpeg-installer/linux-arm64@1.1.0/ffmpeg"
 "https://cdn.jsdelivr.net/npm/ffmpeg-static@5/ffmpeg-linux-arm64"
)
OK=0
for u in "${URLS[@]}"; do
  echo "  尝试: $u"
  if curl -fsSL --max-time 180 "$u" -o "$FF" 2>/dev/null; then
    chmod +x "$FF"
    if file "$FF" 2>/dev/null | grep -qi 'ELF'; then
      echo "  OK 下载成功: $u ($(stat -c%s "$FF") bytes, ELF)"
      OK=1; break
    else
      echo "  XX 非 ELF, 跳过"; rm -f "$FF"; fi
  else
    echo "  失败/超时"; fi
done

# [2] 静态下载失败且有 root -> 尝试 apt
if [ "$OK" -ne 1 ] && [ -n "$SUDO" ]; then
  echo "[2] 静态下载失败, 尝试 apt(root)"
  $SUDO mkdir -p /var/lib/apt/lists/partial
  $SUDO apt-get update -o Acquire::Retries=3 2>&1 | tail -3 || true
  $SUDO apt-get install -y ffmpeg 2>&1 | tail -5 || true
  if command -v ffmpeg >/dev/null 2>&1; then FF=$(command -v ffmpeg); OK=1; fi
fi

# [3] 检查是否含 kmsgrab
echo "[3] 检查 ffmpeg 是否含 kmsgrab"
if [ ! -x "$FF" ]; then echo "  XX 无可用 ffmpeg"; echo "==== DONE ===="; exit 1; fi
echo "  版本: $("$FF" -hide_banner -version 2>/dev/null | head -1)"
if "$FF" -hide_banner -devices 2>&1 | grep -qi kmsgrab; then
  echo "  kmsgrab: YES"
else
  echo "  kmsgrab: NO (此 ffmpeg 不支持, 路不通)"; echo "==== DONE ===="; exit 1
fi

# [4] 实测 kmsgrab 抓一帧 (加 timeout 防卡死)
echo "[4] 实测 kmsgrab 抓一帧 -> /tmp/kms_test.png"
rm -f /tmp/kms_test.png
timeout 40 "$FF" -hide_banner -loglevel info -f kmsgrab -i /dev/dri/card0 -frames:v 1 -y /tmp/kms_test.png 2>&1 | tail -25
echo "---"
if [ -s /tmp/kms_test.png ]; then
  echo "  OK 抓帧成功: $(stat -c%s /tmp/kms_test.png) bytes -> /tmp/kms_test.png"
  file /tmp/kms_test.png 2>/dev/null
else
  echo "  XX 抓帧失败(看上面错误)"
fi
echo "==== DONE ===="
