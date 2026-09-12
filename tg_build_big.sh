#!/bin/bash
# Bring up the big CPU cluster (4-7) and widen affinity, then run the
# USB-GPU big model compile. Equivalent to what build.py does before scons.
set -e

echo "== [1/3] bring cpu4-7 online =="
for i in 4 5 6 7; do
  echo 1 | sudo tee /sys/devices/system/cpu/cpu$i/online >/dev/null 2>&1 || true
done
cat /sys/devices/system/cpu/online

echo "== [2/3] widen shell affinity to 0-7 =="
taskset -pc 0-7 $$ >/dev/null 2>&1 || true
taskset -pc $$

echo "== [3/3] run scons big model build =="
cd /data/openpilot
export PYTHONPATH=/data/openpilot/pydeps:/data/openpilot
export BUILD_USB_GPU_MODEL=1
exec /usr/local/venv/bin/scons -j1 --cache-populate \
  openpilot/selfdrive/modeld/models/big_driving_1791d5940b2c048d_tinygrad.pkl.chunkmanifest
