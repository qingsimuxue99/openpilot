#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="12.4"
fi

export STAGING_ROOT="/data/safe_staging"

# >>> C3 工具箱 看门狗 (由 fix_qr.sh 添加) >>>
[ -x /data/c3_toolbox/c3_watchdog.sh ] && setsid /data/c3_toolbox/c3_watchdog.sh >> /data/c3_toolbox/watchdog.log 2>&1 < /dev/null &
# <<< C3 工具箱 看门狗 <<<
