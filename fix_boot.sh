#!/usr/bin/env bash
# ============================================================
# CarrotPilot 一键修复脚本 v2
#   (开机报错 / 卡开机动画 / 激活失效 / 每次重启都要重新标定)
#
# 用法: 连上 SSH(或打开终端) 后执行
#   curl -fsSL -o /tmp/fix.sh https://cdn.jsdelivr.net/gh/qingsimuxue99/openpilot@CP-DEV/fix_boot.sh && sudo bash /tmp/fix.sh
#
# 覆盖的文件 (4 个, 无条件恢复):
#   cereal/SConscript                            开机编译修复
#   selfdrive/carrot/license.py                  激活码校验核心
#   selfdrive/ui/qt/offroad/settings.cc          激活码输入与状态显示
#   common/params_keys.h                         参数定义(CalibrationParams / CarrotLicStatus)
#
# ------------------------------------------------------------
# v2 改了什么, 以及为什么必须改 (2026-09-20)
# ------------------------------------------------------------
# 1) v1 的 BASE 指向 qingsimuxue99/openpilot@CP-Dev —— 大写 CP-Dev 这个分支根本不存在,
#    源仓库上真正的分支名是 cp-dev。结果 4 个文件全部 404。
# 2) v1 的 curl 没加 -f。jsdelivr 遇到 404 时返回 82 字节英文错误页, 而且 exit code = 0;
#    v1 的守门条件只有 [ "$(wc -c)" -gt 50 ] —— 82 > 50, 校验通过, 于是把错误页 cp 进源码。
#    common/params_keys.h 被写成错误页/错版本之后, 里面就没有 {"CalibrationParams", PERSISTENT}。
# 3) common/params_pyx.so 把 common/params_keys.h 编成了 key 表。
#    每次开机 system/manager/manager.py 都会调 Params.clearAll(CLEAR_ON_MANAGER_START),
#    而 common/params.cc 的 clearAll 会把"不在 key 表里"的参数文件直接 unlink。
#    → key 表里没有 CalibrationParams → 标定文件每次开机被删 → 每次重启都要重新校准。
#    (只丢标定、其它设置都在, 正是这个判据的特征)
# 所以 v2 做了这些事:
#   a) 下载源改为真实存在的 CP-DEV 分支, 并且多源回退
#   b) curl 加 -f: 404/5xx 直接失败, 绝不再把错误页写进源码
#   c) 每个文件下载后按内容特征校验(grep), 校验不过立即停止
#   d) 强制删除并重建 common/params_pyx.so, 重建后再校验 key 表里确实有 CalibrationParams
#   e) 编译依赖(common/params.cc, params.h, SConscript)若缺失会自动补齐
#   f) 只编关键产物 common/params_pyx.so, 不牵连 UI 源码(更快更稳)
# ------------------------------------------------------------
# 设计原则:
#   1. 优先用 git 完整更新; 但取回的树必须先验证是完整的, 否则跳过整体重置(避免删源码)
#   2. 任一步失败立即停止, 保留现场便于排查, 不重启
#   3. 只有编译 + key 表校验都通过才重启
# ============================================================

DIR="/data/openpilot"
SCONS="/usr/local/venv/bin/scons"

# 下载源(按顺序尝试, 第一个通过校验的即采用)。注意大小写必须与分支名完全一致。
BASES="
https://cdn.jsdelivr.net/gh/qingsimuxue99/openpilot@CP-DEV
https://cdn.jsdelivr.net/gh/qsmx9/openpilot@CP-Dev
"

# git 完整更新通道(必须是完整仓库, 否则跳过整体重置)
GIT_URL="https://github.com/qsmx9/openpilot.git"
GIT_BRANCH="CP-Dev"

FILES="
cereal/SConscript
selfdrive/carrot/license.py
selfdrive/ui/qt/offroad/settings.cc
common/params_keys.h
"

# 编译 common/params_pyx.so 的依赖链文件。只在设备上"缺失"时才补, 存在就完全不动。
# 用途: 救"从残缺快照安装、common/ 目录不全"的设备(那种设备本来编不出 params_pyx.so)。
FILLFILES="
common/params.cc
common/params.h
common/SConscript
"

say() { echo; echo "=========== $* ==========="; }
die() { echo; echo "!!! 失败: $*"; echo "!!! 已停止, 未重启。请把上面的全部内容截图发回。"; exit 1; }

# 内容特征校验: $1 = 仓库路径, $2 = 已下载的临时文件
verify() {
  case "$1" in
    common/params_keys.h)
      grep -q 'CalibrationParams' "$2" || return 1
      grep -q 'PERSISTENT' "$2" || return 1 ;;
    selfdrive/ui/qt/offroad/settings.cc)
      grep -q 'CarrotLicStatus' "$2" || return 1 ;;
    selfdrive/carrot/license.py)
      grep -q 'pyarmor' "$2" || return 1 ;;
    cereal/SConscript)
      grep -q 'cereal' "$2" || return 1 ;;
    common/params.cc)
      grep -q 'clearAll' "$2" || return 1 ;;
    common/params.h)
      grep -q 'class Params' "$2" || return 1 ;;
    common/SConscript)
      grep -q 'params_pyx' "$2" || return 1 ;;
  esac
  # 通用毒页检测: jsdelivr 的 404 错误页 / 任何 HTML 一律不通过
  grep -qi "couldn't find the requested file" "$2" && return 1
  grep -qi '<html' "$2" && return 1
  return 0
}

# 从所有下载源里取回一个文件到 $2 (已做尺寸 + 内容校验)
fetch_one() {
  for b in $BASES; do
    rm -f "$2"
    if curl -fsSL --max-time 60 -o "$2" "$b/$1" && [ -s "$2" ] && [ "$(wc -c < "$2")" -gt 200 ]; then
      if verify "$1" "$2"; then
        echo "  取回 $1 ($(wc -c < "$2") 字节) <- $b"
        return 0
      else
        echo "  [跳过] $b/$1 内容校验不通过(疑似 404 错误页或文件损坏)"
      fi
    else
      echo "  [跳过] $b/$1 下载失败"
    fi
  done
  return 1
}

say "0/5 检查环境"
[ -d "$DIR" ] || die "找不到 $DIR, 这不是一台正常的 openpilot 设备"
cd "$DIR" || die "无法进入 $DIR"
echo "当前版本: $(git rev-parse --short HEAD 2>/dev/null)"

# ---------- 1. 优先用 git 完整更新 ----------
say "1/5 尝试用 git 拉取最新代码 (最多等 90 秒)"
if timeout 90 git fetch "$GIT_URL" "$GIT_BRANCH" >/dev/null 2>&1; then
  if git cat-file -e FETCH_HEAD:common/params_keys.h 2>/dev/null && \
     git cat-file -e FETCH_HEAD:launch_chffrplus.sh 2>/dev/null && \
     git cat-file -e FETCH_HEAD:selfdrive/carrot/license.py 2>/dev/null; then
    if git reset --hard FETCH_HEAD >/dev/null 2>&1; then
      echo "git 方式成功, 已更新到: $(git rev-parse --short HEAD)"
    else
      echo "git 重置失败, 改用 CDN 逐文件覆盖"
    fi
  else
    echo "取回的树不完整(疑似残缺快照), 跳过整体重置以免删除源码, 改用 CDN 逐文件覆盖"
  fi
else
  echo "git 直连超时/失败(国内网络常见), 改用 CDN 逐文件覆盖"
fi

# ---------- 2. 兜底/强制: CDN 覆盖关键文件 ----------
say "2/5 恢复关键文件 (开机修复 + 激活相关 + 标定参数定义)"
mkdir -p /data/params 2>/dev/null
for f in $FILES; do
  name=$(basename "$f")
  tmp="/tmp/fix_$name"
  if fetch_one "$f" "$tmp"; then
    cp "$tmp" "$f" || die "写入 $f 失败"
    echo "  已恢复 $f ($(wc -c < "$f") 字节)"
    rm -f "$tmp"
  else
    die "所有下载源都拿不到有效的 $f, 已停止"
  fi
done

# ---------- 2.5 标定参数定义自检(本脚本的关键) ----------
say "2.5/5 自检 common/params_keys.h 必须包含 CalibrationParams"
grep -q 'CalibrationParams' common/params_keys.h || die "common/params_keys.h 里没有 CalibrationParams! 不修好它, 每次开机都会删标定"
grep -n 'CalibrationParams' common/params_keys.h

# ---------- 2.6 补齐缺失的编译依赖(仅当设备上不存在) ----------
say "2.6/5 检查编译依赖是否齐全 (缺了才补, 有就不动)"
for f in $FILLFILES; do
  if [ -f "$f" ]; then
    echo "  已有 $f (不动)"
    continue
  fi
  name=$(basename "$f")
  tmp="/tmp/fix_$name"
  if fetch_one "$f" "$tmp"; then
    mkdir -p "$(dirname "$f")"
    cp "$tmp" "$f" || die "写入 $f 失败"
    echo "  已补齐缺失的 $f ($(wc -c < "$f") 字节)"
    rm -f "$tmp"
  else
    die "设备上缺少 $f 且无法从任何下载源补齐 —— 快照不完整, 请把上面全部内容截图发回"
  fi
done

# ---------- 3. 清理构建产物 ----------
say "3/5 清理可能残缺的编译产物"
rm -rf cereal/gen
echo "已清理 cereal/gen (编译时自动重建)"
rm -f common/params_pyx.so
echo "已删除 common/params_pyx.so (params_keys.h 编进了它的 key 表, 必须强制重建)"

# ---------- 4. 编译 ----------
# 只编关键产物 common/params_pyx.so: 几十秒~几分钟, 且不牵连 UI 源码(避免因无关文件缺失而整体失败)
say "4/5 编译 common/params_pyx.so (关键产物, 期间请勿断电!)"
if "$SCONS" -j4 common/params_pyx.so; then
  echo "单目标编译成功"
else
  echo "单目标编译失败, 回退全量编译(较慢, 约 5-15 分钟, 仍请勿断电)"
  "$SCONS" -j4 || die "编译失败"
fi

say "4.5/5 校验 key 表 (关键)"
grep -a -q 'CalibrationParams' common/params_pyx.so || die "重建后的 params_pyx.so 里没有 CalibrationParams, 结果不对, 已停止"
echo "OK: common/params_pyx.so 已包含 CalibrationParams, 标定不会再被开机清掉"

# ---------- 5. 完成 ----------
say "5/5 编译成功, 即将重启"
echo "编译通过 + key 表校验通过。5 秒后重启设备。"
echo
echo "重启后请注意:"
echo "  - 开机应正常进入界面, 不再出现 failed to build"
echo "  - 若仍显示「未激活」, 请进 设置->授权激活码 重新输入一次激活码"
echo "    (激活状态存在设备参数里, 之前若被清掉需要重新输入一次)"
echo "  - 标定: 这次重启后校准一次即可, 之后不会再每次开机重来"
sleep 5
sudo reboot
