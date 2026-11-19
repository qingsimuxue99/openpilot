#!/usr/bin/env bash

export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMEXPR_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

# models get lower priority than ui
# - ui is ~5ms
# - modeld is 20ms
# - DM is 10ms
# in order to run ui at 60fps (16.67ms), we need to allow
# it to preempt the model workloads. we have enough
# headroom for this until ui is moved to the CPU.
export QCOM_PRIORITY=12

if [ -z "$AGNOS_VERSION" ]; then
  export AGNOS_VERSION="19.8-carrot-bt1"
fi

export STAGING_ROOT="/data/safe_staging"

# eGPU 功率上限：runtime 通过 AM_SMU.set_power_limit() 写 SMU 寄存器生效，
# 属于运行时设置 —— GPU 每次断电重启都会丢，所以必须在开机环境里固化，
# 否则每次开机显卡都是不限功率运行。
export AM_POWER_LIMIT=120
export AM_POWER_LIMIT_PRECOMPILED=120
