#!/usr/bin/bash
# NOTE: -e in the shebang is NOT applied when the script is invoked as
# `bash build_release.sh`, so set it explicitly here. Without it a failed
# `git push` would fall through and the script would still report "Published".
set -e

BUILD_DIR=/data/openpilot
cd $BUILD_DIR

# ---------------------------------------------------------------------------
# Pre-flight.  This script rewrites git history (rm -rf .git), force-pushes the
# egpucp branch and deletes the release/ directory that contains it, so make the
# damage recoverable and keep the tree quiet while it runs:
#   1. stop openpilot, otherwise updated/manager can touch the tree mid-publish
#   2. back up .git and release/ before they are removed
#   3. move pydeps out of the tree.  It is a ~190M runtime artifact that
#      launch_chffrplus.sh rebuilds from the tracked third_party/wheels on the
#      next boot.  git add -f . below ignores .gitignore and would commit it.
#
# Set SKIP_CONFIRM=1 to run unattended, TOKEN=xxx to avoid the
# interactive credential prompt on push.
# ---------------------------------------------------------------------------
if [ -z "$SKIP_CONFIRM" ]; then
  echo "This will: rm -rf .git, force-push branch egpucp to jihulab, delete release/."
  read -r -p "Type yes to continue: " CONFIRM
  if [ "$CONFIRM" != "yes" ]; then
    echo "Aborted."
    exit 1
  fi
fi

echo "==> Stopping openpilot"
pkill -f "[c]omma.sh" 2>/dev/null || true
pkill -f "[l]aunch_chffrplus.sh" 2>/dev/null || true
pkill -f "[m]anager.py" 2>/dev/null || true
tmux kill-server 2>/dev/null || true
sleep 2
echo "    openpilot stopped"

echo "==> Backing up .git and release/"
BK="/data/pre_release_backup_$(date +%m%d_%H%M)"
mkdir -p "$BK"
cp -a "$BUILD_DIR/.git" "$BK/git"
cp -a "$BUILD_DIR/release" "$BK/release"
echo "    saved to $BK"

echo "==> Moving pydeps out of the tree"
if [ -d "$BUILD_DIR/pydeps" ]; then
  rm -rf /data/pydeps_pub_keep
  mv "$BUILD_DIR/pydeps" /data/pydeps_pub_keep
  echo "    pydeps -> /data/pydeps_pub_keep (restored after publish)"
fi

# ---------------------------------------------------------------------------
# Runtime files written inside the source tree must not be published.
# `git add -f .` below overrides every .gitignore (root and per-module), so
# writing them into .gitignore is NOT enough: they have to leave the tree.
#   amapnavi/nav_params.json   UnifiedParams(config.py) 运行时参数文件
#   xiaoge/v_asm_config.json   V-ASM 本地盲区多边形（网页编辑后生成）
# ---------------------------------------------------------------------------
LOCAL_EXCLUDE_PATHS="openpilot/selfdrive/carrot/amapnavi/nav_params.json openpilot/selfdrive/carrot/xiaoge/v_asm_config.json"
LOCAL_KEEP="/data/release_local_keep"
echo "==> Excluding runtime files from the release"
rm -rf "$LOCAL_KEEP"
mkdir -p "$LOCAL_KEEP"
MOVED_LOCAL=""
for p in $LOCAL_EXCLUDE_PATHS; do
  if [ -e "$BUILD_DIR/$p" ]; then
    mkdir -p "$LOCAL_KEEP/$(dirname "$p")"
    mv "$BUILD_DIR/$p" "$LOCAL_KEEP/$p"
    MOVED_LOCAL="$MOVED_LOCAL $p"
    echo "    $p -> $LOCAL_KEEP/$p"
  fi
done
[ -n "$MOVED_LOCAL" ] || echo "    none present"

rm -rf .git
git init
git remote add origin https://jihulab.com/fishop/openpilot.git

# Optional token so the push does not prompt for credentials. It is stripped
# from the remote again right after the push.
if [ -n "$TOKEN" ]; then
  git remote set-url origin "https://fishop:${TOKEN}@jihulab.com/fishop/openpilot.git"
fi

# in the directory
cd $BUILD_DIR

# Cleanup
find . -name '*.a' -delete
find . -name '*.o' -delete
find . -name '*.os' -delete
find . -name '*.pyc' -delete
find . -name 'moc_*' -delete
find . -name '__pycache__' -delete
rm -rf .sconsign.dblite Jenkinsfile release/
#rm -f openpilot/selfdrive/modeld/models/*.onnx
# drop the legacy stamp inside modeld/; it is regenerated at repo root below
rm -f openpilot/selfdrive/modeld/models/.build_stamp

# ship the prebuilt release WITHOUT the .onnx model inputs (the tinygrad .pkl
# artifacts + prebuilt marker are enough). Keep the files on disk so the device
# can still rebuild if ever needed; just don't commit them.
{
  echo 'openpilot/selfdrive/modeld/models/*.onnx'
  # 安全网：`git add -f .` 会无视 .gitignore，真正的排除靠上面把文件移出目录树。
  echo 'openpilot/selfdrive/carrot/amapnavi/nav_params.json'
  echo 'openpilot/selfdrive/carrot/xiaoge/v_asm_config.json'
} > .gitignore

find third_party/ -name '*x86*' -exec rm -r {} +
find third_party/ -name '*Darwin*' -exec rm -r {} +

# Mark as prebuilt release
touch prebuilt

# Add built files to git
git add -f .

VERSION="carrot_v$(date +%y%m%d)"
git commit -m $VERSION
git branch -m "egpucp"

# Recompute .build_stamp against the exact HEAD that will be pushed.
# launch_chffrplus.sh compares this stamp on every boot; if it doesn't match
# the pushed HEAD, FORCE_REBUILD=1 and the device tries to recompile the
# models. But the .onnx inputs are not shipped in this prebuilt release, so the
# build fails and the device hangs. Regenerate the stamp here so the release
# always matches its own HEAD.
# NOTE: the stamp value is the git tree hash of openpilot/selfdrive/modeld, so
# the stamp file itself MUST live outside that tree (repo root). If it lived
# inside modeld/, changing it would change the very hash it records, and the
# stamp could never match after commit.
STAMP="$(git rev-parse HEAD:openpilot/selfdrive/modeld HEAD:tinygrad_repo HEAD:openpilot/common/file_chunker.py | tr '\n' ':')"
echo -n "$STAMP" > .build_stamp
git add -f .build_stamp
git commit -m "${VERSION}-stamp"

# 发布分支里不应再出现被排除的运行时文件
echo "==> Verifying the release carries no excluded runtime files"
FAILED=0
for p in $LOCAL_EXCLUDE_PATHS; do
  # 同时支持目录和文件：匹配该路径本身或其下的内容
  if git ls-files | grep -qE "^${p}(/|$)"; then
    echo "    ERROR: excluded runtime file is still tracked: $p"
    git ls-files | grep -E "^${p}(/|$)" | head -5
    FAILED=1
  fi
done
if [ "$FAILED" = "1" ]; then
  echo "    aborting push; restoring pydeps and runtime files first."
  if [ -d /data/pydeps_pub_keep ]; then
    mv /data/pydeps_pub_keep "$BUILD_DIR/pydeps"
  fi
  for p in $MOVED_LOCAL; do
    if [ -e "$LOCAL_KEEP/$p" ]; then
      mkdir -p "$(dirname "$BUILD_DIR/$p")"
      mv "$LOCAL_KEEP/$p" "$BUILD_DIR/$p"
    fi
  done
  exit 1
fi
echo "    ok: none tracked"

# Capture the result instead of letting `set -e` abort here: the restore steps
# below must always run, otherwise pydeps would stay in /data/pydeps_pub_keep
# and the device would be broken.
PUSH_OK=1
git push -f origin "egpucp" || PUSH_OK=0

# ---------------------------------------------------------------------------
# Post-publish: strip the token from the remote and restore local runtime deps.
# ---------------------------------------------------------------------------
if [ -n "$TOKEN" ]; then
  git remote set-url origin https://jihulab.com/fishop/openpilot.git
fi

echo "==> Restoring pydeps"
if [ -d /data/pydeps_pub_keep ]; then
  mv /data/pydeps_pub_keep "$BUILD_DIR/pydeps"
  echo "    pydeps restored"
fi

echo "==> Restoring runtime files"
for p in $MOVED_LOCAL; do
  if [ -e "$LOCAL_KEEP/$p" ]; then
    mkdir -p "$(dirname "$BUILD_DIR/$p")"
    mv "$LOCAL_KEEP/$p" "$BUILD_DIR/$p"
    echo "    $p restored"
  fi
done
rm -rf "$LOCAL_KEEP" 2>/dev/null || true

if [ "$PUSH_OK" != "1" ]; then
  echo ""
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "!! PUSH FAILED - nothing was published to jihulab."
  echo "!!   pydeps 已恢复，设备可正常使用。"
  echo "!!   常见原因：jihulab 未配置凭据。可先 git config credential.helper store"
  echo "!!   或设置 TOKEN=<token> 后重跑本脚本。"
  echo "!!   注意：本次已 rm -rf .git 并重建为 egpucp 分支，"
  echo "!!         如需回到原分支，可从 $BK 恢复 .git。"
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  exit 1
fi

echo ""
echo "Published branch egpucp to jihulab as $VERSION."
echo "Pre-publish backup: $BK"
echo "Now reboot to restart openpilot:  sudo reboot"
