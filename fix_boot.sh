#!/usr/bin/env bash
# ============================================================
# CarrotPilot 一键修复脚本 (开机报错 / 卡开机动画 / 激活失效)
#
# 适用:
#   1) 更新后界面停在 "openpilot failed to build" 或卡在开机动画
#   2) 付费激活失效 / 显示未激活
#
# 用法: 连上 SSH(或打开终端), 粘贴这一行执行
#   curl -sL https://cdn.jsdelivr.net/gh/qingsimuxue99/openpilot@CP-Dev/fix_boot.sh | bash
#
# 覆盖的文件 (4 个):
#   开机修复: cereal/SConscript
#   激活相关: selfdrive/carrot/license.py        激活码校验核心
#             selfdrive/ui/qt/offroad/settings.cc 激活码输入与状态显示
#             common/params_keys.h                CarrotLicStatus/Remain 参数定义
#
# 设计原则:
#   1. 优先用 git 完整更新; git 直连超时(国内常见)则走 CDN 逐文件覆盖
#   2. 每个文件下载后校验非空且大小合理, 校验不过立即停止, 绝不写入坏文件
#   3. 只有编译完全通过才重启; 任一步失败立即停止, 保留现场便于排查
# ============================================================

DIR="/data/openpilot"
SCONS="/usr/local/venv/bin/scons"
BASE="https://cdn.jsdelivr.net/gh/qingsimuxue99/openpilot@CP-Dev"

# 需要用仓库版本强制覆盖的文件
FILES="
cereal/SConscript
selfdrive/carrot/license.py
selfdrive/ui/qt/offroad/settings.cc
common/params_keys.h
"

say() { echo; echo "=========== $* ==========="; }
die() { echo; echo "!!! 失败: $*"; echo "!!! 已停止, 未重启。请把上面的全部内容截图发回。"; exit 1; }

say "0/5 检查环境"
[ -d "$DIR" ] || die "找不到 $DIR, 这不是一台正常的 openpilot 设备"
cd "$DIR" || die "无法进入 $DIR"
echo "当前版本: $(git rev-parse --short HEAD 2>/dev/null)"

# ---------- 1. 优先用 git 完整更新 ----------
say "1/5 尝试用 git 拉取最新代码 (最多等 90 秒)"
if timeout 90 git fetch https://github.com/qingsimuxue99/openpilot.git CP-Dev >/dev/null 2>&1; then
  git reset --hard FETCH_HEAD >/dev/null 2>&1 && echo "git 方式成功, 已更新到: $(git rev-parse --short HEAD)" || die "git 重置失败"
  GIT_OK=1
else
  echo "git 直连超时/失败(国内网络常见), 改用 CDN 逐文件覆盖"
  GIT_OK=0
fi

# ---------- 2. 兜底/强制: CDN 覆盖关键文件 ----------
say "2/5 恢复关键文件 (开机修复 + 激活相关)"
mkdir -p /data/params 2>/dev/null
for f in $FILES; do
  name=$(basename "$f")
  tmp="/tmp/fix_$name"
  if curl -sL --max-time 60 -o "$tmp" "$BASE/$f" && [ -s "$tmp" ] && [ "$(wc -c < "$tmp")" -gt 50 ]; then
    cp "$tmp" "$f" || die "写入 $f 失败"
    echo "  已恢复 $f ($(wc -c < "$f") 字节)"
    rm -f "$tmp"
  else
    die "下载 $f 失败或内容异常, 已停止"
  fi
done

# ---------- 3. 清理构建产物 ----------
say "3/5 清理可能残缺的编译产物"
rm -rf cereal/gen
echo "已清理 cereal/gen (编译时自动重建)"

# ---------- 4. 编译 ----------
say "4/5 开始编译 (约 5-15 分钟, 期间请勿断电!)"
"$SCONS" -j4 || die "编译失败"

# ---------- 5. 完成 ----------
say "5/5 编译成功, 即将重启"
echo "编译全部通过。5 秒后重启设备。"
echo
echo "重启后请注意:"
echo "  - 开机应正常进入界面, 不再出现 failed to build"
echo "  - 若仍显示「未激活」, 请进 设置→授权激活码 重新输入一次激活码"
echo "    (激活状态存在设备参数里, 之前若被清掉需要重新输入一次)"
sleep 5
sudo reboot
