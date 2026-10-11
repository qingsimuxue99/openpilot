"""Fetch the precompiled artifacts as soon as the ONNX is in place.

build.py runs earlier in the launch sequence and skips the precompiled fetch
while the ONNX is still missing, which used to leave it until the next boot.
Calling this right after ensure_big_model() makes one boot enough.

Kept separate from big_model.py so upstream refactors of that script cannot
clobber the logic.
"""

import json
import sys
from pathlib import Path

# Boot starts this downloader before DNS is necessarily up. The launcher's own network wait
# covers the manifest host only, while the precompiled catalog is served from the model host
# (manifest.url). A cold boot can therefore resolve one and not the other, and the fetch used
# to be deferred to the next boot - a delivery the user explicitly selected must not ride on
# one DNS hiccup. Wait for the host we are about to talk to, and retry a few times.
CATALOG_WAIT_SECONDS = 90.0
FETCH_ATTEMPTS = 3
FETCH_RETRY_DELAY = 20.0

# Touched once the download is complete, and read by the HUD badge to show
# "REBOOT". /tmp is a tmpfs: a reboot (or power loss) wipes it, so the prompt
# disappears on its own. Timestamps cannot be used for this - the clock is
# reset across power cycles.
REBOOT_FLAG = Path("/tmp/carrot-big-model-needs-reboot")

# The stock model_runtime.py only looks for AMD firmware in /lib/firmware, which is
# read-only on AGNOS. firmware.py downloads the blobs to /data/media/0/carrot/firmware,
# so without this patch tinygrad cannot find them, falls back to downloading from
# gitlab (403), and the boot validation then rejects the whole artifact set.
# Install.sh applies the same patch; this is the equivalent for the HTTP path.
_FW_OLD = '''  def fetch_fw(path, name, sha256):
    p = pathlib.Path(f"/lib/firmware/{path}/{name}.zst")
    if p.is_file():
      blob = zstandard.ZstdDecompressor().stream_reader(p.read_bytes()).read()
      if hashlib.sha256(blob).hexdigest() == sha256:
        return blob
    return _orig(path, name, sha256)'''

_FW_NEW = '''  # Firmware search path: read-only system dir, the persistent /data dir and the blobs
  # shipped in-tree for this board. This file is extracted into the model cache, so the
  # checkout cannot be located from __file__: probe both layouts it may have.
  import pathlib
  _FW_BASES = ["/lib/firmware", "/data/media/0/carrot/firmware"]
  for _cand in ("/data/openpilot/system/hardware/chestnut/firmware",
                "/data/openpilot/openpilot/system/hardware/chestnut/firmware"):
    if pathlib.Path(_cand).is_dir() and _cand not in _FW_BASES:
      _FW_BASES.append(_cand)
  try:
    from openpilot.common.basedir import BASEDIR
    _FW_ROOT = pathlib.Path(BASEDIR)
    for _cand in (_FW_ROOT / 'system' / 'hardware' / 'chestnut' / 'firmware',
                  _FW_ROOT / 'openpilot' / 'system' / 'hardware' / 'chestnut' / 'firmware'):
      if _cand.is_dir() and str(_cand) not in _FW_BASES:
        _FW_BASES.append(str(_cand))
  except Exception:
    pass
  _FW_BASES = tuple(_FW_BASES)
  def fetch_fw(path, name, sha256):
    for base in _FW_BASES:
      p = pathlib.Path(base) / path / (name + ".zst")
      if p.is_file():
        blob = zstandard.ZstdDecompressor().stream_reader(p.read_bytes()).read()
        if hashlib.sha256(blob).hexdigest() == sha256:
          return blob
    return _orig(path, name, sha256)'''

# The generic (Cinque v3) runtime ships plain tinygrad instead of model_runtime.py, so
# there is no `_orig` to replace: append a wrapper around the fetch_fw it defines. The
# wrapper is appended at the end of helpers.py (after the definition) and marked, so a
# second pass is a no-op. Everything is guarded: if the wrapper cannot import what it
# needs it falls through to the original function, i.e. to upstream's behaviour.
_FW_HELPERS_WRAPPER = '''

# --- fork patch: AMD firmware search paths -------------------------------------
# Appended by precompiled_fetch.patch_runtime_firmware_paths(). This runtime only looks
# in /lib/firmware (the AGNOS image ships a different linux-firmware revision than the
# one this runtime pins) and otherwise downloads from gitlab, which the device cannot
# reach (403) - so the eGPU fails to initialise even though the model is valid. Look
# where firmware.ensure_firmware() stores the verified blobs and, when present, at the
# blobs shipped in-tree for this board.
if not globals().get('_FORK_FW_WRAPPED'):
  import hashlib as _FORK_HS
  import pathlib as _FORK_PL
  _FORK_FW_BASES = ['/lib/firmware', '/data/media/0/carrot/firmware']
  # The checkout path is not derivable from inside an extracted runtime, so probe both
  # layouts the in-tree blobs may live at (same candidates as firmware.py).
  for _FORK_CAND in ('/data/openpilot/system/hardware/chestnut/firmware',
                     '/data/openpilot/openpilot/system/hardware/chestnut/firmware'):
    if _FORK_PL.Path(_FORK_CAND).is_dir() and _FORK_CAND not in _FORK_FW_BASES:
      _FORK_FW_BASES.append(_FORK_CAND)
  try:
    from openpilot.common.basedir import BASEDIR as _FORK_BASEDIR
    _FORK_ROOT = _FORK_PL.Path(_FORK_BASEDIR)
    for _FORK_CAND in (str(_FORK_ROOT / 'system' / 'hardware' / 'chestnut' / 'firmware'),
                       str(_FORK_ROOT / 'openpilot' / 'system' / 'hardware' / 'chestnut' / 'firmware')):
      if _FORK_PL.Path(_FORK_CAND).is_dir() and _FORK_CAND not in _FORK_FW_BASES:
        _FORK_FW_BASES.append(_FORK_CAND)
  except Exception:
    pass
  _FORK_FW_ORIG = fetch_fw

  def fetch_fw(path, name, sha256):  # noqa: F811 - intentional wrapper around upstream's
    for _base in _FORK_FW_BASES:
      _p = _FORK_PL.Path(_base) / path / (name + '.zst')
      if not _p.is_file():
        continue
      _blob = None
      try:
        import zstandard as _FORK_ZSTD
        _blob = _FORK_ZSTD.ZstdDecompressor().stream_reader(_p.read_bytes()).read()
      except Exception:
        try:
          from compression.zstd import decompress as _FORK_DZSTD
          _blob = _FORK_DZSTD(_p.read_bytes())
        except Exception:
          continue
      if _FORK_HS.sha256(_blob).hexdigest() == sha256:
        return _blob
    return _FORK_FW_ORIG(path, name, sha256)

  _FORK_FW_WRAPPED = True
'''


def patch_runtime_firmware_paths(model=None, cache_dir=None) -> bool:
  """Teach the extracted runtime to look for firmware under /data as well.

  Two layouts are handled: model_runtime.py (the run-model runtime, replaced in place)
  and the generic runtime that ships plain tinygrad/ only (helpers.py, wrapped).
  """
  try:
    from openpilot.selfdrive.modeld.big_model import active_manifest, model_cache_dir

    m = model or active_manifest()
    if m is None:
      return False
    root = (cache_dir or model_cache_dir()) / 'precompiled' / m.sha256
    info = json.loads((root / 'installed.json').read_text(encoding='utf-8'))
    runtime = root / info['runtime_directory']

    target = runtime / 'model_runtime.py'
    if target.is_file():
      src = target.read_text(encoding='utf-8')
      if '_FW_BASES' in src:
        return True
      if _FW_OLD not in src:
        from openpilot.common.swaglog import cloudlog
        cloudlog.warning('firmware path patch: unexpected model_runtime.py, skipped')
        return False
      target.write_text(src.replace(_FW_OLD, _FW_NEW), encoding='utf-8')
      return True

    helpers = runtime / 'tinygrad' / 'helpers.py'
    if helpers.is_file():
      src = helpers.read_text(encoding='utf-8')
      if '_FORK_FW_WRAPPED' in src:
        return True
      helpers.write_text(src + _FW_HELPERS_WRAPPER, encoding='utf-8')
      return True

    return False
  except Exception:
    return False


def _precompiled_rejected(manifest) -> bool:
  """Was this model's precompiled artifact rejected on this device?

  precompiled_model.reject() writes the marker after a permanent boot-validation failure.
  For a precompiled-only model (the generic PKL that Cinque v3 ships as) that is the only
  possible writer: sync_precompiled_marker() only runs once a chunked set was installed,
  and such a model can never have one. So for those models the marker always means "this
  artifact does not run on this hardware", and clearing it would only retry a known-bad
  runtime on every boot.
  """
  try:
    from openpilot.selfdrive.modeld.big_model import model_cache_dir
    return (Path(model_cache_dir()) / 'precompiled' / manifest.sha256 / 'rejected').is_file()
  except Exception:
    return False


def _wait_for_host(url: str, timeout: float) -> bool:
  """Bounded wait until the host behind `url` resolves and accepts connections."""
  if not url:
    return False
  try:
    from openpilot.selfdrive.modeld.big_model import wait_for_manifest_network
    return bool(wait_for_manifest_network(url, timeout))
  except Exception:
    return False


def precompiled_required(manifest) -> bool:
  """Can only the precompiled artifact satisfy the current selection?

  Either the user pinned the precompiled delivery, or the model is a generic PKL: the
  precompiled artifact *is* the model file then, with no ONNX to compile locally and no
  chunked set to split. Both mean the boot cannot call itself "ready" while it is missing.
  """
  if bool(getattr(manifest, 'precompiled_only', False)):
    return True
  try:
    from openpilot.selfdrive.modeld.chunked_model import read_model_source
    return read_model_source() == 'precompiled'
  except Exception:
    return False


def _ensure_precompiled_with_retry(manifest, progress):
  """ensure_precompiled() with a host wait and bounded retries; raises on failure."""
  from urllib.parse import urljoin

  from openpilot.selfdrive.modeld.precompiled_model import ensure_precompiled

  try:
    catalog_url = urljoin(manifest.url, 'precompiled.json')
  except Exception:
    catalog_url = ''
  _wait_for_host(catalog_url, CATALOG_WAIT_SECONDS)
  last_error = None
  for attempt in range(1, FETCH_ATTEMPTS + 1):
    try:
      return ensure_precompiled(manifest, progress=progress)
    except Exception as exc:
      last_error = exc
      print(f"precompiled model fetch failed ({attempt}/{FETCH_ATTEMPTS}): {exc}", file=sys.stderr)
      if attempt < FETCH_ATTEMPTS:
        _wait_for_host(catalog_url, FETCH_RETRY_DELAY)
  raise OSError(f"precompiled model unavailable after {FETCH_ATTEMPTS} attempts: {last_error}")


def fetch_after_onnx(manifest, reporter) -> None:
  """Download the big-model artifacts for `manifest`, reporting progress.

  Never raises for an optional delivery: a failure there must not prevent the normal
  internal-GPU build. A delivery the user selected (pinned precompiled/chunked, or a
  precompiled-only model) does raise, because the launcher retries this downloader only
  while status.json says "error" - see the callers in launch_chffrplus.sh.

  Both deliveries download here, in the background, for the same reason: the boot build
  must not sit on a multi-minute download, and the HUD/web pages get their progress from
  status.json either way.
  """
  source = "auto"
  fetched_chunked = False
  newly_installed = False
  # A precompiled-only model *is* the model file: it ships no ONNX to compile locally and
  # no chunked set to split, so the precompiled artifact is the only delivery that can ever
  # produce it. A pinned "chunked" therefore cannot be honoured for it - honouring it would
  # download the model and then leave it unusable - and the pin keeps deciding the delivery
  # of every model that does have a chunked alternative.
  only_precompiled = bool(getattr(manifest, 'precompiled_only', False))
  try:
    from openpilot.selfdrive.modeld import chunked_model

    source = chunked_model.read_model_source()
    if source != "precompiled" and not only_precompiled:
      def chunked_progress(done, total) -> None:
        # detail="chunked model" maps to the same "PKL" label as the precompiled set.
        reporter.update("downloading", model_id=manifest.model_id, sha256=manifest.sha256,
                        downloaded_bytes=done, total_bytes=total, detail="chunked model")

      # A reboot is only needed when something actually landed in this run: this function
      # runs on every boot, and touching the flag unconditionally would leave the "REBOOT"
      # prompt on screen forever.
      was_installed = chunked_model.installed_chunked(manifest) is not None
      fetched_chunked = chunked_model.ensure_chunked(manifest, progress=chunked_progress) is not None
      newly_installed = fetched_chunked and not was_installed
  except Exception as exc:
    print(f"chunked model fetch deferred: {exc}", file=sys.stderr)
    if source == "chunked" and not only_precompiled:
      # A pinned delivery that did not land has to be visible: the launcher retries this
      # downloader only while status.json says "error". "auto" keeps the local-compiler
      # fallback and therefore stays a silent deferral, as before.
      raise RuntimeError(f"chunked model fetch failed: {exc}") from exc

  if fetched_chunked:
    # Steer the (untouched) resolution to the chunked set and prompt for the reboot that
    # makes modeld pick it up.
    try:
      chunked_model.sync_precompiled_marker(manifest)
      if newly_installed:
        REBOOT_FLAG.touch()
    except Exception:
      pass  # purely cosmetic; never fail over a flag file
    return
  if source == "chunked" and not only_precompiled:
    return  # the user pinned this delivery; the boot build falls back to the local compile
  if only_precompiled and _precompiled_rejected(manifest):
    # Say why instead of leaving the HUD on "ready": there is nothing to fetch, and
    # ensure_precompiled() would skip the download for the same reason. The caller turns
    # this into status.json's "error" (and its own message says the same), which is what
    # makes the pages show the reason instead of a silent "ready".
    reporter.update("error", model_id=manifest.model_id, sha256=manifest.sha256,
                    detail="precompiled runtime was rejected on this device")
    raise RuntimeError('precompiled runtime was rejected on this device')
  if source == "precompiled":
    # The user pinned this delivery: drop the marker that makes ensure_precompiled() skip the
    # download. Without this the pin resolves to the chunked set that is already installed
    # (rejected marker -> installed() == None -> the old precompiled-first resolution fell
    # through), i.e. "I picked the precompiled package but the car still drives the chunked
    # one". An explicit pin is also an explicit retry, so a rejection recorded for a previous
    # artifact does not block the install - a permanent runtime failure is re-recorded by
    # record_failure() during this boot, and the web UI shows it instead of hiding it.
    try:
      chunked_model.sync_precompiled_marker(manifest)
    except Exception:
      pass  # purely cosmetic; the download below is what matters
  elif source == "auto" or only_precompiled:
    # Chunked is unavailable: let the precompiled artifact count as installed again.
    try:
      chunked_model.clear_precompiled_marker(manifest)
    except Exception:
      pass

  try:
    from openpilot.selfdrive.modeld.firmware import ensure_firmware

    def precompiled_progress(done, total) -> None:
      # detail="pkl" is what the HUD badge keys off to label this phase "PKL"
      # instead of "ONNX" (the ONNX download is reported by big_model itself and
      # carries no detail field).
      reporter.update("downloading", model_id=manifest.model_id, sha256=manifest.sha256,
                      downloaded_bytes=done, total_bytes=total, detail="pkl")

    # Firmware is tiny and must be on disk before the first GPU init of the boot.
    # Upstream's ensure_precompiled used to fetch it; this fork keeps the call in
    # this fork-only module so precompiled_model.py stays upstream's file.
    ensure_firmware(manifest)
    installed = _ensure_precompiled_with_retry(manifest, precompiled_progress)
    if installed is None and precompiled_required(manifest):
      # ensure_precompiled() declines while the artifact being served is the very one this
      # device rejected earlier; name that instead of leaving the pages on "ready".
      raise OSError('precompiled artifact is rejected on this device')
    # The runtime is extracted by now; teach it where the firmware lives.
    patch_runtime_firmware_paths(manifest)
    try:
      REBOOT_FLAG.touch()
    except Exception:
      pass  # purely cosmetic; never fail over a flag file
  except Exception as exc:
    # The launcher retries this downloader only while status.json says "error"
    # (launch_chffrplus.sh: big_model_update_failed), and the HUD/web pages take the reason
    # from the same file. An optional delivery stays a silent deferral; one the user
    # selected must be reported, or "I picked the precompiled package" ends up as "nothing
    # ever downloaded until the next boot" - which is exactly what this used to do.
    if precompiled_required(manifest):
      raise RuntimeError(f"precompiled model fetch failed: {exc}") from exc
    print(f"precompiled model fetch deferred: {exc}", file=sys.stderr)
