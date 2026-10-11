"""Fork-side runtime patches for the USB-eGPU stack.

These patches used to live inside upstream files (modeld.py, usbgpu.py,
compile_modeld.py, precompiled_model.py, helpers.py). They are collected here so those
files stay upstream's, and so every process that needs them gets them from one place: the
repo-root ``sitecustomize.py`` imports and applies this module at interpreter start
(see that file for why the hook works for manager-spawned processes, the precompiled
worker subprocess and the ``check_usbgpu`` probe alike).

What is patched, and why the patches are needed at all:

* ``AM_POWER_LIMIT``: the chestnut (ASM24) board defaults to the GPU's SMU PPT limit
  (~182W). 120W lowers the peak current and cuts USB/PCIe link flapping under load,
  which showed up as ``bulk IN`` I/O errors mid-inference. tinygrad's amdev reads it.
* ``tinygrad.helpers.fetch_fw``: tinygrad only looks for AMD firmware in
  ``/lib/firmware`` and otherwise downloads it from gitlab. That directory holds a
  different linux-firmware build than the one tinygrad pins, the download is blocked
  (HTTP 403), and the failed init then disables the eGPU. Also search the persistent
  dir ``firmware.ensure_firmware()`` fills and the blobs shipped in-tree for this board.
* ``precompiled_model.record_failure``: a dropped bulk transfer or a lost USB lock race
  is a property of the link, not of the artifact. Judging those permanent blacklisted
  healthy precompiled artifacts until someone deleted the marker by hand.
* ``helpers.active_usbgpu_compiled_path`` / ``usbgpu_compile_pending``: upstream resolves
  "precompiled first, chunked second", so pinning the precompiled package in the web UI
  silently resolved to the chunked artifact whenever the precompiled one was missing or
  rejected - the page said one delivery while the car drove the other. Under that pin the
  two functions answer for the precompiled artifact only (see patch_model_delivery).

Rules for anything added here:

* never import the modules being patched at startup - use ``patch_after_import()``. A
  process that never touches tinygrad must not pay for it, and modeld must keep setting
  ``GMMU`` before its first tinygrad import;
* one patch per concern, idempotent, and silent on failure: this module runs in every
  python process on the device and must never be able to break one;
* ``OPENPILOT_EGPU_PATCH=0`` disables everything.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import os
import pathlib
import sys
from collections.abc import Callable

ENABLED = os.environ.get("OPENPILOT_EGPU_PATCH", "1") != "0"

AM_POWER_LIMIT = ""  # the chunked model runs uncapped; see AM_POWER_LIMIT_PRECOMPILED below
# The precompiled worker gets a cap instead of the default above. Every generic-onnx artifact
# we tested dies with "bulk IN 0x81 failed: Input/Output Error" at full clocks on two C3s, while
# the same artifact is stable for hours with a power wall. The chunked model runs through this
# repo's own tinygrad, which retries those transfers, and has run uncapped for hours.
AM_POWER_LIMIT_PRECOMPILED = "120"

# Firmware search order for fetch_fw: the read-only system dir AGNOS ships, then the
# persistent dir the model server fills. The in-tree blobs (see firmware_bases()) are
# appended when this checkout has them.
FIRMWARE_BASES = (
  "/lib/firmware",
  "/data/media/0/carrot/firmware",
)

# USB transport failures that mean "the link hiccupped", not "the artifact is broken".
TRANSIENT_ERROR_TEXT = (
  "bulk IN ",
  "bulk OUT ",
  "Input/Output Error",
  "libusb_",
  "usb bridge reset",
  "Failed to acquire lock file",
)

# Upstream's record_failure already treats this wording as transient; wrapping the
# message keeps one definition of "transient" instead of a second, drifting copy.
_TRANSIENT_WRAPPER_TEXT = "precompiled eGPU worker timed out"


def in_tree_firmware_candidates() -> tuple[pathlib.Path, ...]:
  """Where the blobs shipped with this checkout may live, most likely first.

  Resolved from this file's own location first: it sits at <package>/selfdrive/modeld,
  so two levels up is the package root. BASEDIR is only a second guess, because it points
  at the checkout root and the package may be one directory deeper (as on the device).
  """
  candidates = [pathlib.Path(__file__).resolve().parents[2] / "system" / "hardware" / "chestnut" / "firmware"]
  try:
    from openpilot.common.basedir import BASEDIR

    basedir = pathlib.Path(BASEDIR)
    candidates += [basedir / "system" / "hardware" / "chestnut" / "firmware",
                   basedir / "openpilot" / "system" / "hardware" / "chestnut" / "firmware"]
  except Exception:
    pass
  return tuple(dict.fromkeys(candidates))


def firmware_bases() -> tuple[str, ...]:
  """Search bases in order: system, persistent cache, then the in-tree blobs."""
  bases = list(FIRMWARE_BASES)
  for candidate in in_tree_firmware_candidates():
    if candidate.is_dir():
      bases.append(str(candidate))
  if len(bases) == len(FIRMWARE_BASES):
    # Not fatal here, but the in-tree blobs are the only copy guaranteed to match the
    # runtime's pinned hashes: without them an unreachable mirror leaves no way to start
    # the eGPU, so this must be visible instead of quietly degrading.
    try:
      from openpilot.common.swaglog import cloudlog

      cloudlog.warning("eGPU firmware: no in-tree blobs found (looked in "
                       + ", ".join(str(path) for path in in_tree_firmware_candidates()) + ")")
    except Exception:
      pass
  return tuple(dict.fromkeys(bases))


def _decompress(raw: bytes) -> bytes | None:
  """Decompress a .zst firmware blob, or None when no decompressor is importable.

  AGNOS's system python has no zstandard; the openpilot venv does, and Python 3.14
  gained compression.zstd. Returning None keeps the caller on the upstream path.
  """
  try:
    import zstandard  # type: ignore[import-untyped]

    return zstandard.ZstdDecompressor().stream_reader(raw).read()
  except Exception:
    pass
  try:
    from compression.zstd import decompress  # type: ignore[import-not-found]

    return decompress(raw)
  except Exception:
    return None


def _is_precompiled_worker() -> bool:
  """True inside the subprocess that owns the AMD device for a precompiled artifact.

  That worker is exec'd as ``<python> .../precompiled_worker.py <pkl> ...`` and is the only
  process that initialises the eGPU for a precompiled delivery, so the power cap is decided
  there: a delivery switch then needs no restart, and the chunked model keeps running uncapped.
  """
  return any("precompiled_worker.py" in str(arg) for arg in sys.argv[:2])


def apply_env_defaults() -> bool:
  """Inject AM_POWER_LIMIT for the delivery that is actually about to use the eGPU.

  Precompiled worker -> AM_POWER_LIMIT_PRECOMPILED (override with the same env var, e.g.
  AM_POWER_LIMIT_PRECOMPILED="" for a full-speed A/B). Anything else (the chunked model in
  modeld) -> no cap unless AM_POWER_LIMIT is set by the environment.
  """
  changed = False
  if _is_precompiled_worker():
    want = os.environ.get("AM_POWER_LIMIT_PRECOMPILED", AM_POWER_LIMIT_PRECOMPILED)
    if want and os.environ.get("AM_POWER_LIMIT") != want:
      os.environ["AM_POWER_LIMIT"] = want
      changed = True
    elif not want and "AM_POWER_LIMIT" in os.environ:
      del os.environ["AM_POWER_LIMIT"]
      changed = True
  elif AM_POWER_LIMIT and os.environ.get("AM_POWER_LIMIT") is None:
    os.environ["AM_POWER_LIMIT"] = AM_POWER_LIMIT
    changed = True
  return changed
def patch_fetch_fw(helpers) -> bool:
  """Wrap tinygrad's fetch_fw so the local firmware dirs are searched first."""
  if getattr(helpers, "_fork_fw_bases", None) is not None:
    return False
  original = helpers.fetch_fw
  bases = firmware_bases()

  def fetch_fw(path, name, sha256):
    for base in bases:
      candidate = pathlib.Path(base) / path / f"{name}.zst"
      if not candidate.is_file():
        continue
      blob = _decompress(candidate.read_bytes())
      if blob is not None and hashlib.sha256(blob).hexdigest() == sha256:
        return blob
    return original(path, name, sha256)

  helpers.fetch_fw = fetch_fw
  helpers._fork_fw_bases = bases
  return True


def patch_record_failure(precompiled_model) -> bool:
  """Report USB link hiccups with the wording upstream already treats as transient."""
  if getattr(precompiled_model, "_fork_transient_wrapper", False):
    return False
  original = precompiled_model.record_failure

  def record_failure(path, error, phase):
    detail = str(error)
    if _TRANSIENT_WRAPPER_TEXT not in detail and any(text in detail for text in TRANSIENT_ERROR_TEXT):
      # The original detail is still what gets persisted; only the classification is
      # borrowed from the worker-timeout wording the upstream function understands.
      error = TimeoutError(f"{_TRANSIENT_WRAPPER_TEXT}: {detail}")
    return original(path, error, phase)

  precompiled_model.record_failure = record_failure
  precompiled_model._fork_transient_wrapper = True
  return True


def _selected_manifest():
  """The model that may execute, asked of the module that defines it.

  Not ``helpers.active_manifest``: that name was only readable on ``helpers`` because
  upstream imported it there, and upstream has since renamed it (``selected_manifest``)
  and dropped that import, which made every call below raise AttributeError at interpreter
  start (ui_state resolves the compiled path while importing). Reading the definition site
  keeps this file out of upstream's import list, which is the whole reason it exists.
  """
  from openpilot.selfdrive.modeld.big_model import selected_manifest
  return selected_manifest()


def patch_model_delivery(helpers) -> bool:
  """Make upstream's artifact resolution honour the pinned delivery in both directions.

  helpers.py is upstream's file, so the pin is applied here instead of in it. Upstream
  resolves "precompiled first, chunked second", which on its own honours neither pin:

  * pinned precompiled: the chunked set is usually on disk too, so upstream fell through to
    it and substituted it silently. Under this pin the two functions below answer for the
    precompiled artifact only - None and "pending" while it is absent - which is what lets
    the web UI and the HUD explain the mismatch instead of hiding it.
  * pinned chunked: an installed precompiled package wins upstream, and the `rejected`
    marker that would make `precompiled_model.installed()` return None only appears after a
    failed boot - nothing creates it for a chunked pin. So the chunked artifact is preferred
    here whenever it is installed, and a missing one is reported as pending instead of
    being silently served from the precompiled package.

  `auto` is documented as "chunked first, precompiled when there is none", so it takes the
  same preference and only differs by falling back. A precompiled-only model (a generic
  PKL) has no chunked set to pick, so it keeps upstream's answer under every pin.
  """
  if getattr(helpers, "_fork_model_delivery", False):
    return False
  original_active = helpers.active_usbgpu_compiled_path
  original_pending = helpers.usbgpu_compile_pending

  def pinned_source():
    """The configured delivery pin, or 'auto' when it cannot be read."""
    try:
      from openpilot.selfdrive.modeld.chunked_model import read_model_source
      return read_model_source()
    except Exception:
      return "auto"

  def pinned_precompiled():
    """(model, installed artifact) while the precompiled delivery is pinned, else (None, None)."""
    if pinned_source() != "precompiled":
      return None, None
    try:
      from openpilot.selfdrive.modeld.precompiled_model import installed
    except Exception:
      return None, None
    model = _selected_manifest()
    if model is None:
      return None, None
    try:
      return model, installed(model)
    except Exception:
      return None, None

  def chunked_artifact(model):
    """The installed chunked artifact for `model`, or None when its set is missing."""
    if model is None or model.precompiled_only:
      return None
    try:
      from openpilot.common.file_chunker import get_manifest_path
      path = helpers.modeld_pkl_path(usbgpu=True, model_sha256=model.sha256)
      return path if pathlib.Path(get_manifest_path(path)).is_file() else None
    except Exception:
      return None

  def active_usbgpu_compiled_path():
    if pinned_source() != "precompiled":
      # 'auto' and 'chunked' both want the chunked artifact first; only the answer for a
      # missing set differs, and a precompiled-only model has no chunked set either way.
      model = _selected_manifest()
      if model is not None and not model.precompiled_only:
        chunked = chunked_artifact(model)
        if chunked is not None:
          return chunked
        if pinned_source() == "chunked":
          return None  # pinned to chunked and not built yet: never substitute precompiled
    model, precompiled = pinned_precompiled()
    return precompiled if model is not None else original_active()

  def usbgpu_compile_pending():
    if pinned_source() == "chunked":
      model = _selected_manifest()
      if model is not None and not model.precompiled_only and chunked_artifact(model) is None:
        return True  # the boot build still has to produce it, so nothing usable is here yet
    model, precompiled = pinned_precompiled()
    return precompiled is None if model is not None else original_pending()

  helpers.active_usbgpu_compiled_path = active_usbgpu_compiled_path
  helpers.usbgpu_compile_pending = usbgpu_compile_pending
  helpers._fork_model_delivery = True
  return True


def patch_after_import(module_name: str, callback: Callable[[object], bool]) -> bool:
  """Call callback(module) as soon as module_name finishes importing.

  Used instead of importing the target here: the call sites import tinygrad and
  precompiled_model lazily, and importing them at interpreter start would be a
  behaviour change (startup cost, GMMU ordering), not a patch.
  """
  if (module := sys.modules.get(module_name)) is not None:
    try:
      callback(module)
    except Exception:
      pass
    return True

  class _Finder:
    def find_spec(self, fullname, path=None, target=None):
      if fullname != module_name:
        return None
      sys.meta_path.remove(self)
      try:
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
      except Exception:
        return None
      if spec is None or spec.loader is None:
        return None
      execute = spec.loader.exec_module

      def exec_module(module):
        execute(module)
        try:
          callback(module)
        except Exception:
          pass

      try:
        spec.loader.exec_module = exec_module  # type: ignore[method-assign]
      except Exception:
        return None
      return spec

  sys.meta_path.insert(0, _Finder())
  return True


def apply() -> list[str]:
  """Apply every patch, returns the names that changed something (for logging/tests)."""
  if not ENABLED:
    return []
  applied: list[str] = []
  try:
    if apply_env_defaults():
      applied.append("env:AM_POWER_LIMIT")
  except Exception:
    pass
  try:
    if patch_after_import("tinygrad.helpers", patch_fetch_fw):
      applied.append("hook:tinygrad.helpers")
  except Exception:
    pass
  try:
    if patch_after_import("openpilot.selfdrive.modeld.precompiled_model", patch_record_failure):
      applied.append("hook:precompiled_model")
  except Exception:
    pass
  try:
    if patch_after_import("openpilot.selfdrive.modeld.helpers", patch_model_delivery):
      applied.append("hook:helpers")
  except Exception:
    pass
  return applied
