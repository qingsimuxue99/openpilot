"""AMD firmware blobs the USB eGPU needs, fetched from the model server.

This lives in its own module on purpose: precompiled_model.py should only call
ensure_firmware(), so pulling upstream changes into that file stays conflict-free.

The blobs are looked up by the compiled runtime's fetch_fw patch under
/data/media/0/carrot/firmware (see model_runtime.py). Without them the eGPU never
initializes, even when the model itself is valid.

Part of that set also ships in-tree (system/hardware/chestnut/firmware/amdgpu). A blob the
server does not serve is staged from the checkout when it matches the requested hash, so a
mirror publishing an incomplete set cannot leave the eGPU unable to initialise. Existing
files are never rewritten: the two runtimes on this board pin different builds of the same
names and share this directory.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.modeld.big_model import active_manifest

FIRMWARE_DIR = Path('/data/media/0/carrot/firmware/amdgpu')

# Optional manifest published next to the blobs. When present it takes priority,
# so shipping a model for a different GPU arch needs no code change here.
_FIRMWARE_MANIFEST = 'manifest.json'

# name -> (sha256 of the .zst exactly as served, size in bytes)
# The blobs the runtimes on this board ask for. The first four are the build the precompiled
# (pkl) runtime pins and the model server serves; the last four are identical in both the
# runtime's build and the one shipped in-tree, so either copy satisfies them. Anything the
# server publishes overrides these per name (see _remote_firmware).
FIRMWARE: dict[str, tuple[str, int]] = {
  'smu_14_0_2.bin.zst':    ('4c80055939f89ce619a8aaf71882fa04d426279191f893ff32acf02dc4861476', 137291),
  'gc_12_0_0_me.bin.zst':  ('ef7fbca61215ae42dbb96048467793e7f7a06de139e4df7121be11d70750ebd6', 47877),
  'gc_12_0_0_mec.bin.zst': ('b0d5ac1728f43873f03d94f27c8e9807e42e42653fb76054577dbd08befeb43e', 82417),
  'gc_12_0_0_pfp.bin.zst': ('aae909f6c0481b2761a4193f1ed26e793676f83abb8db16e966402ecf120b8c4', 41901),
  'gc_12_0_0_rlc.bin.zst': ('01806109f63d4fe294b4bd108a34a0439c9a9568d45f71a039ef476ff9cb3010', 48374),
  'gc_12_0_0_imu.bin.zst': ('aaabca90b09db1b3f90f9ca717be19af0bd2f3295cfe6c227bf835c5428c3fb4', 21297),
  'psp_14_0_2_sos.bin.zst': ('42646102d34b9005e705831027ddf27ec952d5542dedd652ad82338f3b8e8ff9', 184938),
  'sdma_7_0_0.bin.zst':    ('cb68fe7868ea11440c93cf03b0b67da635b963b7a020055b6ce4c8f844236ec7', 18184),
}

# Firmware sits next to the model root, one level above the model file's directory.
_FIRMWARE_PREFIX = '../firmware/amdgpu/'

# This file lives in <package>/selfdrive/modeld, so two levels up is the package root and
# the blobs shipped with the checkout are at <package>/system/hardware/chestnut/firmware.
# Resolving it from __file__ keeps working whatever BASEDIR points at.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _in_tree_candidates() -> tuple[Path, ...]:
  """Where the shipped blobs may live, most likely first."""
  candidates = [_PACKAGE_ROOT / 'system' / 'hardware' / 'chestnut' / 'firmware']
  try:
    from openpilot.common.basedir import BASEDIR

    base = Path(BASEDIR)
    candidates += [base / 'system' / 'hardware' / 'chestnut' / 'firmware',
                   base / 'openpilot' / 'system' / 'hardware' / 'chestnut' / 'firmware']
  except Exception:
    pass
  return tuple(dict.fromkeys(candidates))


def in_tree_firmware_dir() -> Path | None:
  """Directory holding the blobs shipped with this checkout, or None when absent."""
  for candidate in _in_tree_candidates():
    if (candidate / 'amdgpu').is_dir():
      return candidate
  return None


def _parse_manifest(raw) -> dict[str, tuple[str, int]] | None:
  """name -> (sha256 of the .zst, size) from a manifest body, or None when unusable."""
  files = raw.get('files') if isinstance(raw, dict) else None
  if not isinstance(files, dict):
    return None
  out: dict[str, tuple[str, int]] = {}
  for name, meta in files.items():
    if not isinstance(name, str) or not isinstance(meta, dict):
      continue
    sha, size = meta.get('sha256'), meta.get('size')
    if isinstance(sha, str) and len(sha) == 64 and isinstance(size, int) and size > 0:
      out[name] = (sha, size)
  return out or None


def _remote_firmware(model) -> dict[str, tuple[str, int]] | None:
  """Firmware list published by the server next to the blobs, if any.

  Lets a model built for a different GPU arch ship its own firmware without a
  code change here. Any problem simply falls back to the built-in list.
  """
  url = urljoin(model.url, _FIRMWARE_PREFIX + _FIRMWARE_MANIFEST)
  try:
    with urlopen(Request(url, headers={'Accept-Encoding': 'identity'}), timeout=15) as response:
      if response.status != 200:
        return None
      raw = json.loads(response.read().decode('utf-8'))
  except Exception:
    return None
  return _parse_manifest(raw)


def _stage_in_tree(directory: Path, name: str, target: Path, want_hash: str) -> bool:
  """Place a blob shipped with the checkout in the missing slot `target`.

  The blob the runtimes ask for is verified against the requested hash, so a checkout that
  carries a different build of the same file is simply skipped instead of poisoning the
  shared firmware directory. Only ever called for a file that is not there yet.
  """
  source = directory / 'amdgpu' / name
  try:
    raw = source.read_bytes()
  except OSError:
    return False
  if hashlib.sha256(raw).hexdigest() != want_hash:
    cloudlog.warning(f'in-tree firmware {name} is a different build than the one requested; skipping')
    return False
  target.write_bytes(raw)
  cloudlog.info(f'firmware {name} staged from the checkout')
  return True


def _fetch(url: str, target: Path, size: int, want_hash: str) -> None:
  """Minimal verified download, used only when no downloader is supplied."""
  partial = target.with_suffix(target.suffix + '.part')
  with urlopen(Request(url, headers={'Accept-Encoding': 'identity'}), timeout=30) as response:
    if response.status != 200:
      raise OSError(f'unexpected download status {response.status}')
    with partial.open('wb') as f:
      written = 0
      while data := response.read(1024 * 1024):
        written += len(data)
        if written > size:
          raise ValueError('firmware exceeds declared size')
        f.write(data)
      f.flush()
      os.fsync(f.fileno())
  if written != size:
    raise OSError('incomplete firmware download')
  with partial.open('rb') as f:
    if hashlib.file_digest(f, 'sha256').hexdigest() != want_hash:
      partial.unlink()
      raise ValueError('firmware hash mismatch')
  os.replace(partial, target)


def _report_progress(done: int, total: int) -> None:
  """Publish the firmware phase so the HUD badge can show "FW xx%".

  The blobs are tiny (~300 KB total), so this updates once per blob rather than
  streaming. Purely cosmetic: any failure here is ignored.
  """
  try:
    from openpilot.selfdrive.modeld.big_model import model_cache_dir
    from openpilot.selfdrive.modeld.big_model_status import write_big_model_status

    write_big_model_status(model_cache_dir(), 'downloading',
                           downloaded_bytes=done, total_bytes=total,
                           detail='firmware')
  except Exception:
    pass


def ensure_firmware(model=None, download: Callable[[dict, Path], None] | None = None) -> bool:
  """Make sure every firmware blob is present and verified.

  download is called as download(artifact, target) with artifact containing
  url/size/sha256; pass precompiled_model.download to reuse its resume logic.
  Already-present blobs are skipped, so this is cheap to call every time.
  Returns True when all blobs are in place.
  """
  model = model or active_manifest()
  if model is None:
    return False

  # The server's list wins per name: it publishes the build the precompiled (pkl) runtime
  # pins. The built-in list supplies the blobs the server does not serve, which are also
  # the ones shipped in-tree, so a gap can be filled without touching the network.
  firmware = {**FIRMWARE, **(_remote_firmware(model) or {})}
  in_tree = in_tree_firmware_dir()

  FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
  total = sum(size for _, size in firmware.values())
  done = 0
  ok = True
  for name, (want_hash, want_size) in sorted(firmware.items()):
    target = FIRMWARE_DIR / name
    artifact = {'url': urljoin(model.url, _FIRMWARE_PREFIX + name),
                'size': want_size, 'sha256': want_hash}
    # Never rewrite a blob that is already there: the same directory feeds the extracted pkl
    # runtime and the locally compiled one, and those two pin different builds of the same
    # names, so a file that looks wrong here may be exactly what the other runtime needs.
    if target.is_file():
      if target.stat().st_size != want_size:
        cloudlog.warning(f'keeping existing firmware {name} ({target.stat().st_size} bytes); '
                         f'the requested build is {want_size} bytes')
      continue
    try:
      if in_tree is not None and _stage_in_tree(in_tree, name, target, want_hash):
        pass
      elif in_tree is None:
        cloudlog.warning(f'firmware {name} is missing and no in-tree copy is available; '
                         'the eGPU cannot start without it')
        ok = False
        continue
      elif download is not None:
        download(artifact, target)
      else:
        _fetch(artifact['url'], target, want_size, want_hash)
    except Exception as exc:
      # Never discard an otherwise valid model over a firmware mirror problem,
      # but it must be visible: the eGPU cannot start without these.
      cloudlog.warning(f'precompiled firmware {name} unavailable: {exc}')
      ok = False
      continue
    # Only report when something was actually fetched. Otherwise a normal boot
    # with all blobs present would leave status.json stuck on "downloading".
    done += want_size
    _report_progress(done, total)
  return ok
