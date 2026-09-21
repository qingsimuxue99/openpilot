#!/bin/bash
# C3 工具箱 - 开机自启脚本 (代码随仓库分发: /data/openpilot/selfdrive/carrot/toolbox)
# 运行时数据(log/pid/qr.png)统一放 /data/c3_toolbox, 避免污染 git 工作区
# flask 装在 /data/pylibs (系统分区 /usr/local 只读, 不可写)

TOOLBOX_DIR="/data/openpilot/selfdrive/carrot/toolbox"   # 代码位置(仓库内)
RUNTIME_DIR="/data/c3_toolbox"                           # 运行时数据(仓库外)
mkdir -p "$RUNTIME_DIR"
LOG_FILE="$RUNTIME_DIR/server.log"
PID_FILE="$RUNTIME_DIR/server.pid"

# --- 诊断守护 diagd 自启 (随仓库分发, 记录所有故障/错误, 输出到 /data/c3_toolbox/diag) ---
# 放在工具箱 PID_FILE 检查之前: 确保无论工具箱是否已运行, diagd 都会被拉起,
# 朋友 clone CP-Dev 分支开机即自动记录故障, 无需手动配置
# 由 launch_chffrplus.sh 开机调用本脚本
DIAGD_DIR="/data/openpilot/selfdrive/carrot"
DIAGD_LOG="$RUNTIME_DIR/diag"
mkdir -p "$DIAGD_LOG"
if ! pgrep -f "[d]iagd.py" >/dev/null 2>&1; then
    nohup setsid /usr/local/venv/bin/python3 "$DIAGD_DIR/diagd.py" >> "$DIAGD_LOG/daemon.log" 2>&1 < /dev/null &
    echo "[$(date)] 诊断守护 diagd 已启动 (PID: $!)" >> "$LOG_FILE"
else
    echo "[$(date)] 诊断守护 diagd 已在运行, 跳过" >> "$LOG_FILE"
fi

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

# 选择 python：优先 venv，回退 python3
PYP=/usr/local/venv/bin/python
[ -x "$PYP" ] || PYP=python3

# 启动工具箱 (cd 代码目录, 让 SCRIPT_DIR 定位 html; PYTHONPATH 加仓库内 pylibs 供 flask)
cd "$TOOLBOX_DIR"
export PYTHONPATH=/data/openpilot/selfdrive/carrot/toolbox/pylibs:${PYTHONPATH:-}
setsid "$PYP" c3_toolbox_local.py >> "$LOG_FILE" 2>&1 < /dev/null &
echo $! > "$PID_FILE"

echo "[$(date)] 工具箱已启动 (PID: $!)" >> "$LOG_FILE"
