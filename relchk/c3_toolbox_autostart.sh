#!/bin/bash
# C3 工具箱 - 开机自启脚本
# 部署到设备后，将本脚本内容追加到系统启动脚本中实现开机自动运行

TOOLBOX_DIR="/data/c3_toolbox"
LOG_FILE="$TOOLBOX_DIR/server.log"
PID_FILE="$TOOLBOX_DIR/server.pid"

# 如果已在运行就跳过
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "[$(date)] 工具箱已在运行 (PID: $OLD_PID)" >> "$LOG_FILE"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# 等待网络就绪 (最多等 30 秒)
for i in $(seq 1 30); do
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "$IP" ]; then
        break
    fi
    sleep 1
done

# 选择 python：优先 venv（装了 flask），回退 python3
PYP=/usr/local/venv/bin/python
[ -x "$PYP" ] || PYP=python3

# 启动工具箱
cd "$TOOLBOX_DIR"
setsid "$PYP" c3_toolbox_local.py >> "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

echo "[$(date)] 工具箱已启动 (PID: $!)" >> "$LOG_FILE"