"""Fork hook: apply the USB-eGPU runtime patches in every python process.

CPython imports this module automatically at interpreter start (for ``python -c``,
``python -m`` and scripts alike) as long as this directory is on ``sys.path``. The
device launcher puts the checkout root there through ``PYTHONPATH``
(launch_chffrplus.sh), which the manager and its children inherit - including the
precompiled worker subprocess and the ``check_usbgpu`` probe. That makes this the one
place where a patch can run before user code without editing an upstream file.

Keep this file tiny and defensive: it runs in every python process on the device
(pytest and the modeld worker included), so it only delegates, and any failure is
swallowed. Set ``OPENPILOT_EGPU_PATCH=0`` to disable the patches.

See openpilot/selfdrive/modeld/egpu_patches.py for what is applied and why.
"""

import os
import sys

# --- agnos tool bridge -------------------------------------------------------
# /etc/profile sets PYTHONPATH=/data/pythonpath (a symlink to this checkout), which
# exposes the openpilot tree but NOT its vendored dependencies. launch_chffrplus.sh
# adds "$DIR/pydeps" itself, so openpilot works -- but agnos' own tools do not go
# through that script. /usr/comma/comma.sh runs /usr/comma/reset directly, so the
# factory-reset UI loaded this tree (openpilot/system/hardware/tici/hardware.py ->
# lpa.py) and died with "No module named 'serial'", leaving reset unable to start.
# Appending (never prepending) preserves normal resolution order and only supplies
# packages that would otherwise be missing.
_PYDEPS = "/data/openpilot/pydeps"
if os.path.isdir(_PYDEPS) and _PYDEPS not in sys.path:
  sys.path.append(_PYDEPS)

if os.environ.get("OPENPILOT_EGPU_PATCH", "1") != "0":
  try:
    from openpilot.selfdrive.modeld import egpu_patches

    egpu_patches.apply()
  except Exception:
    pass
