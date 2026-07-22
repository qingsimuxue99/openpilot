#!/bin/bash
# ============================================================
#  C3 工具箱 · 一键部署脚本
#  适用: comma c3 设备, 已运行 openpilot
#  作用: 从 jsdelivr 下载最新发布包 -> 解压 /data/c3_toolbox -> 启动
#  用法:
#     bash install_c3_toolbox.sh                 # 装最新版
#     bash install_c3_toolbox.sh v1.0.73         # 装指定版本
#     bash install_c3_toolbox.sh --autostart     # 写开机自启
#     bash install_c3_toolbox.sh --reboot        # 部署后自动重启
#     bash install_c3_toolbox.sh --reboot --yes  # 重启不确认
# ============================================================
set -e

REPO="qingsimuxue99/openpilot"
INSTALL_DIR="/data/c3_toolbox"
LOG_FILE="$INSTALL_DIR/server.log"
PID_FILE="$INSTALL_DIR/server.pid"
AUTO_START=0
SPEC_TAG=""
DO_REBOOT=0
ASSUME_YES=0

for arg in "$@"; do
  case "$arg" in
    --autostart) AUTO_START=1 ;;
    --reboot)    DO_REBOOT=1 ;;
    --yes|-y)    ASSUME_YES=1 ;;
    v[0-9]*)     SPEC_TAG="$arg" ;;
  esac
done

echo "============================================"
echo "        C3 工具箱 一键部署"
echo "============================================"

# 1) 确定版本
echo ""
echo ">> [1/6] 确定版本..."
if [ -n "$SPEC_TAG" ]; then
  TAG="$SPEC_TAG"
  echo "   使用指定版本: $TAG"
else
  echo "   查询最新版本..."
  TAG=$(curl -fsSL "https://data.jsdelivr.com/v1/package/gh/$REPO" 2>/dev/null \
        | grep -oE '"name":"v[0-9]+\.[0-9]+\.[0-9]+"' | head -1 \
        | sed -E 's/"name":"(v[0-9.]+)"/\1/')
  if [ -z "$TAG" ]; then
    TAG="v1.0.73"
    echo "   未能实时查询到最新版，回退到 $TAG"
  else
    echo "   最新版本: $TAG"
  fi
fi

# 2) 创建目录 + 3) 下载
URL="https://cdn.jsdelivr.net/gh/$REPO@$TAG/release/c3_toolbox.tar.gz"
TMP_TAR="/tmp/c3_toolbox_$TAG.tar.gz"
echo ""
echo ">> [2/6] 创建目录 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
echo ">> [3/6] 下载发布包 (进度条如下)"
echo "   $URL"
curl -# -fSL "$URL" -o "$TMP_TAR"
if [ ! -s "$TMP_TAR" ]; then
  echo "   [错误] 下载失败或文件为空，请检查设备能否访问 cdn.jsdelivr.net"
  exit 1
fi
echo "   下载完成: $(du -h "$TMP_TAR" 2>/dev/null | cut -f1)"

# 4) 解压
echo ""
echo ">> [4/6] 解压到 $INSTALL_DIR ..."
tar xzf "$TMP_TAR" -C "$INSTALL_DIR"
rm -f "$TMP_TAR"
echo "   已释放文件: $(ls -1 "$INSTALL_DIR" | wc -l) 个"

# 5) python + flask
echo ""
echo ">> [5/6] 准备运行环境"
PYP="/usr/local/venv/bin/python"
if [ ! -x "$PYP" ]; then
  if command -v python3 >/dev/null 2>&1; then PYP="python3"; else echo "   [错误] 未找到 python3"; exit 1; fi
fi
echo "   使用 Python: $PYP"
if ! "$PYP" -c "import flask" 2>/dev/null; then
  echo "   未检测到 flask，正在安装..."
  "$PYP" -m pip install flask --quiet 2>/dev/null \
    || pip3 install flask --quiet 2>/dev/null \
    || { echo "   [错误] flask 安装失败，请手动: $PYP -m pip install flask"; exit 1; }
fi

# 6) 启动
echo ""
echo ">> [6/6] 启动服务"
pkill -f "c3_toolbox_local.py" 2>/dev/null || true
sleep 1
cd "$INSTALL_DIR"
setsid "$PYP" c3_toolbox_local.py >> "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"
sleep 2

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo ""
echo "============================================"
echo "        部署完成!"
echo "   本机:   http://127.0.0.1:5588"
[ -n "$IP" ] && echo "   局域网: http://$IP:5588"
echo "   日志:   $LOG_FILE"
echo "============================================"

# 开机自启 (可选)
setup_autostart() {
  local target="/data/start.sh"
  if [ -f "$target" ] && ! grep -q "c3_toolbox" "$target"; then
    cat >> "$target" <<EOF

# >>> C3 工具箱 开机自启 (由 install_c3_toolbox.sh 添加) >>>
( sleep 20; bash $INSTALL_DIR/c3_toolbox_autostart.sh ) &
# <<< C3 工具箱 开机自启 <<<
EOF
    echo "已写入开机自启: $target"
  else
    echo "未找到 $target 或已配置自启，跳过。"
  fi
}
if [ "$AUTO_START" -eq 1 ]; then
  setup_autostart
fi

# 自动重启 (可选)
if [ "$DO_REBOOT" -eq 1 ]; then
  if [ "$ASSUME_YES" -eq 1 ]; then
    echo ""
    echo "5 秒后自动重启 (--yes)..."
    sleep 5
    sudo reboot
  else
    echo ""
    echo "部署完成。5 秒后自动重启以生效 (按 Ctrl+C 可取消)..."
    sleep 5
    sudo reboot
  fi
fi
