#!/bin/bash
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
echo "==== comma c3 投屏: weston-screenshooter 检查/启用 ($([ $APPLY -eq 1 ] && echo 应用模式 || echo 仅检查)) ===="
SHOOTER=/usr/libexec/weston-screenshooter
INI=/usr/comma/weston.ini

echo "[root] uid=$(id -u)"
echo "[1] 客户端"; [ -x "$SHOOTER" ] && echo "✓ $SHOOTER" || { echo "✗ 不存在"; exit 1; }
echo "[2] screenshooter.so 模块(关键)"; SO=$(find / -xdev -name 'screenshooter.so' 2>/dev/null | head -1); [ -n "$SO" ] && echo "✓ $SO" || echo "✗ 缺失(仅客户端无用, 需改用 kmsgrab)"
echo "[3] weston 默认 module-path"; weston --help 2>&1 | grep -i 'module-path' | head -2
echo "[4] weston.ini [core] 段"; grep -iA3 '^\[core\]' "$INI" 2>/dev/null || echo "(无 [core] 段)"
echo "[5] 是否已配置 screenshooter 模块?"; grep -qi 'modules=.*screenshooter' "$INI" 2>/dev/null && echo "✓ 已配" || echo "✗ 未配"
echo "[6] /dev/video* (v4l2 备选)"; ls /dev/video* 2>/dev/null || echo "(无)"
echo "[7] libdrm 开发库(写底层抓取工具用)"; pkg-config --exists libdrm && echo "✓ libdrm 可链接 ($(pkg-config --modversion libdrm))" || echo "✗ 无 libdrm-dev"
echo "[8] apt-get?"; command -v apt-get >/dev/null && apt-get --version 2>/dev/null | head -1 || echo "✗ 无 apt-get"
echo "[9] weston-screenshooter 帮助(参数参考)"; timeout 4 "$SHOOTER" --help 2>&1 | head -15

if [ $APPLY -eq 1 ]; then
  echo; echo ">>> 应用模式: 启用 screenshooter <<<"
  [ "$(id -u)" -ne 0 ] && { echo "✗ 需 root 运行"; exit 1; }
  [ -z "$SO" ] && { echo "✗ 模块缺失, 无法启用(改走 kmsgrab 方案)"; exit 1; }
  MP=$(weston --help 2>&1 | grep -i 'module-path' | grep -oE '/[^ ]+' | head -1)
  if [ -n "$MP" ] && [ "$(dirname "$SO")" != "$MP" ] && [ ! -e "$MP/screenshooter.so" ]; then
    ln -sf "$SO" "$MP/screenshooter.so" && echo "✓ 软链模块到 $MP/"
  fi
  if ! grep -qi 'modules=.*screenshooter' "$INI" 2>/dev/null; then
    cp -a "$INI" "${INI}.bak.$(date +%s)"
    if grep -qi '^\[core\]' "$INI"; then sed -i '/^\[core\]/a modules=screenshooter.so' "$INI"; else printf '\n[core]\nmodules=screenshooter.so\n' >> "$INI"; fi
    echo "✓ 已加 modules=screenshooter.so 到 $INI (已备份原文件)"
  fi
  echo "→ 重启 weston (屏幕会短暂黑屏, UI 通常自动恢复)..."; systemctl restart weston.service 2>/dev/null || systemctl restart weston 2>/dev/null || { echo "✗ 重启失败"; exit 1; }
  sleep 5
  export XDG_RUNTIME_DIR=/run/user/0; [ -d /run/user/0 ] || export XDG_RUNTIME_DIR=/tmp
  export WAYLAND_DISPLAY=wayland-0
  OUT=/tmp/ws_test.png; rm -f "$OUT"
  timeout 8 "$SHOOTER" -o "$OUT" 2>/tmp/ws_err.log
  if [ -s "$OUT" ]; then echo "✓✓ 截图成功 $(stat -c%s "$OUT")B 头=$(head -c8 "$OUT"|xxd -p)"; else echo "✗ 失败:"; cat /tmp/ws_err.log; fi
else
  echo; echo "检查完成。若 [2]✓ 且 [5]✗, 运行: sudo bash /tmp/ws_setup.sh --apply"
fi
echo "==== DONE ===="
