#!/usr/bin/env bash

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null && pwd )"

source "$DIR/launch_env.sh"
export PATH="/usr/local/venv/bin:$PATH"

function agnos_init {
  # TODO: move this to agnos
  sudo rm -f /data/etc/NetworkManager/system-connections/*.nmmeta
  rm -f /data/scons_cache/config.lock

  # set success flag for current boot slot
  sudo abctl --set_success

  # TODO: do this without udev in AGNOS
  # udev does this, but sometimes we startup faster
  sudo chgrp gpu /dev/adsprpc-smd /dev/ion /dev/kgsl-3d0
  sudo chmod 660 /dev/adsprpc-smd /dev/ion /dev/kgsl-3d0

  # Check if AGNOS update is required
  if [ $(< /VERSION) != "$AGNOS_VERSION" ]; then
    AGNOS_PY="$DIR/openpilot/common/hardware/comma/agnos.py"
    MANIFEST="$DIR/openpilot/system/hardware/comma/agnos.json"
    if $AGNOS_PY --verify $MANIFEST; then
      sudo reboot
    fi
    while true; do
      $DIR/openpilot/common/hardware/comma/updater $AGNOS_PY $MANIFEST
    done
  fi
}

function launch {
  # Remove orphaned git lock if it exists on boot
  [ -f "$DIR/.git/index.lock" ] && rm -f $DIR/.git/index.lock

  # Check to see if there's a valid overlay-based update available. Conditions
  # are as follows:
  #
  # 1. The DIR init file has to exist, with a newer modtime than anything in
  #    the DIR Git repo. This checks for local development work or the user
  #    switching branches/forks, which should not be overwritten.
  # 2. The FINALIZED consistent file has to exist, indicating there's an update
  #    that completed successfully and synced to disk.

  if [ -f "${DIR}/.overlay_init" ]; then
    find ${DIR}/.git -newer ${DIR}/.overlay_init | grep -q '.' 2> /dev/null
    if [ $? -eq 0 ]; then
      echo "${DIR} has been modified, skipping overlay update installation"
    else
      if [ -f "${STAGING_ROOT}/finalized/.overlay_consistent" ]; then
        if [ ! -d /data/safe_staging/old_openpilot ]; then
          echo "Valid overlay update found, installing"
          LAUNCHER_LOCATION="${BASH_SOURCE[0]}"

          mv $DIR /data/safe_staging/old_openpilot
          mv "${STAGING_ROOT}/finalized" $DIR
          cd $DIR

          echo "Restarting launch script ${LAUNCHER_LOCATION}"
          unset AGNOS_VERSION
          exec "${LAUNCHER_LOCATION}"
        else
          echo "openpilot backup found, not updating"
          # TODO: restore backup? This means the updater didn't start after swapping
        fi
      fi
    fi
  fi

  # handle pythonpath
  ln -sfn $(pwd) /data/pythonpath
  export PYTHONPATH="$PWD"

  # submodule package symlinks for PYTHONPATH imports on device.
  # on PC these come from editable installs via pyproject.toml / uv.
  ln -sfn msgq_repo/msgq msgq
  ln -sfn opendbc_repo/opendbc opendbc
  ln -sfn rednose_repo/rednose rednose
  ln -sfn teleoprtc_repo/teleoprtc teleoprtc
  ln -sfn tinygrad_repo/tinygrad tinygrad

  # The bundled AGNOS updater zipapp imports pyserial (tici/lpa.py inside the
  # archive). agnos_init may invoke that updater, so make sure serial is
  # importable first. Offline wheel in third_party/wheels is tried before the
  # PyPI fallback. Installs into $DIR/pydeps without touching the device venv.
  if [ -f /AGNOS ]; then
    # Put pydeps on PYTHONPATH unconditionally BEFORE the import test and the
    # AGNOS updater run. Previously the export was gated on an import test that
    # ran without pydeps on PYTHONPATH, so it never fired even when the wheel
    # install succeeded, and the updater still crashed on `import serial`.
    PYDEPS="${PYDEPS:-$DIR/pydeps}"
    mkdir -p "$PYDEPS"
    case ":$PYTHONPATH:" in
      *":$PYDEPS:"*) ;;
      *) export PYTHONPATH="$PYDEPS:$PYTHONPATH" ;;
    esac
    if ! python3 -c "import serial" > /dev/null 2>&1; then
      echo "[bootstrap] pyserial missing before AGNOS check; installing..."
      wheel_dir="$DIR/third_party/wheels"
      if [ -d "$wheel_dir" ] && ls "$wheel_dir"/pyserial*.whl > /dev/null 2>&1; then
        echo "[bootstrap] pyserial: installing from local wheel..."
        python3 -m pip install --no-index --no-deps --find-links "$wheel_dir" --target "$PYDEPS" --upgrade pyserial > /tmp/pip_pyserial.log 2>&1
      fi
      if ! python3 -c "import serial" > /dev/null 2>&1; then
        echo "[bootstrap] pyserial: installing from PyPI (needs network)..."
        python3 -m pip install --target "$PYDEPS" --upgrade pyserial > /tmp/pip_pyserial.log 2>&1
      fi
      if python3 -c "import serial" > /dev/null 2>&1; then
        echo "[bootstrap] pyserial: ok"
      else
        echo "[bootstrap] pyserial NOT available; AGNOS updater may fail importing serial"
      fi
    fi
  fi

  # hardware specific init
  if [ -f /AGNOS ]; then
    agnos_init
  fi

  # write tmux scrollback to a file
  tmux capture-pane -pq -S-1000 > /tmp/launch_log

  # start manager
  # 启动 C3 工具箱（优先仓库内副本，工具箱随仓库安装；/data 旧副本仅兜底）
  # TOOLBOX_DELAYED：延时启动 —— 等设备正常进入 UI（ui 进程存活并稳定 10s）后再拉起，
  # 避免开机阶段与 manager/编译抢 CPU；最多等 5 分钟兜底（UI 起不来也照常启动）。
  TOOLBOX_PY="$DIR/openpilot/system/toolbox/c3_toolbox_local.py"
  [ -f "$TOOLBOX_PY" ] || TOOLBOX_PY="/data/c3_toolbox_local.py"
  (
    waited=0
    while [ "$waited" -lt 300 ]; do
      if pgrep -f "selfdrive\.ui\.ui" > /dev/null 2>&1; then
        sleep 10
        break
      fi
      sleep 5
      waited=$((waited + 5))
    done
    echo "[toolbox] UI ready (waited ${waited}s), starting toolbox" >> /tmp/toolbox.log
    # TOOLBOX_PYTHONPATH：补上 cereal/pydeps 路径，否则开机启动的感知采集
    # 会因 import cereal 失败而一直 available:false
    PYTHONPATH="$DIR:/data/openpilot/pydeps:${PYTHONPATH:-}" \
      nohup python3 "$TOOLBOX_PY" >> /tmp/toolbox.log 2>&1 &
  ) &

  cd openpilot/system/manager
  if [ ! -f $DIR/prebuilt ] && [ ! -f /data/params/d/SkipOnroadCompile ]; then
    ./build.py
  fi
  ./manager.py

  # if broken, keep on screen error
  while true; do sleep 1; done
}

launch
