"""Multiple big-model coexistence: index, selection and resolution.

Several big models can live side by side under the model cache dir
(/data/media/0/carrot/models/). This module keeps the small index that maps each
downloaded ONNX to its full manifest, remembers which one the user picked in the
web UI, and resolves that pick into a BigModelManifest for the downloader.

Kept in its own file so upstream edits to big_model.py stay tiny: only
fetch_manifest() needs a forward to selected_manifest() here.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

INDEX_FILENAME = 'models.json'
# Delivery filenames this branch can have on disk: the pinned Cinque v2 ONNX and the
# generic precompiled PKL that the optional Cinque v3 ships as (see model_source).
MODEL_GLOBS = ('big_driving_supercombo-*.onnx', 'big_driving_tinygrad-*.pkl')
ONNX_GLOB = MODEL_GLOBS[0]  # kept for callers that still reference the ONNX glob


def _cache_dir(cache_dir: Path | None = None) -> Path:
  if cache_dir is not None:
    return Path(cache_dir)
  from openpilot.selfdrive.modeld.big_model import model_cache_dir
  return Path(model_cache_dir())


def index_path(cache_dir: Path | None = None) -> Path:
  return _cache_dir(cache_dir) / INDEX_FILENAME


def read_index(cache_dir: Path | None = None) -> dict:
  try:
    value = json.loads(index_path(cache_dir).read_text(encoding='utf-8'))
    if isinstance(value, dict) and isinstance(value.get('models'), dict):
      return value
  except (OSError, ValueError, TypeError):
    pass
  return {'selected': None, 'models': {}}


def write_index(value: dict, cache_dir: Path | None = None) -> None:
  path = index_path(cache_dir)
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, tmp = tempfile.mkstemp(prefix='.models-', suffix='.json', dir=path.parent)
  try:
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
      json.dump(value, f, indent=2, sort_keys=True)
      f.write('\n')
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)
  finally:
    try:
      os.unlink(tmp)
    except FileNotFoundError:
      pass


def register(manifest, cache_dir: Path | None = None, *, selected: bool | None = None) -> None:
  """Record a downloaded manifest so it can be listed and switched to later."""
  value = read_index(cache_dir)
  value['models'][manifest.sha256] = {
    'model_id': manifest.model_id,
    'sha256': manifest.sha256,
    'size': manifest.size,
    'url': manifest.url,
    'filename': manifest.filename,
  }
  if selected is True or value.get('selected') is None:
    value['selected'] = manifest.sha256
  write_index(value, cache_dir)


def register_remote(entry: dict, cache_dir: Path | None = None) -> bool:
  """Record a model advertised by the server catalog, before it is downloaded.

  register() only runs once a download succeeded, so without this a device that
  has never fetched a model would reject the user's pick as "unknown model", and
  its URL would stay frozen at whatever it was first registered with - a
  server-side layout change would never reach it. The entry is only stored when
  it yields a valid manifest, so a malformed catalog cannot poison the index.
  """
  if not isinstance(entry, dict):
    return False
  sha, url, filename, size = (entry.get('sha256'), entry.get('url'),
                              entry.get('filename'), entry.get('size'))
  if not isinstance(sha, str) or not isinstance(url, str) or not url:
    return False
  if not isinstance(filename, str) or not filename:
    return False
  if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
    return False
  meta = {'model_id': entry.get('model_id'), 'filename': filename,
          'size': size, 'sha256': sha, 'url': url}
  try:
    from openpilot.selfdrive.modeld.big_model import BigModelManifest
    BigModelManifest.from_dict(meta, url)
  except Exception:
    return False
  value = read_index(cache_dir)
  value['models'][sha] = meta
  write_index(value, cache_dir)
  return True


def _sync_from_state(cache_dir: Path | None = None) -> dict:
  """Make sure active/previous from state.json are listed in the index."""
  value = read_index(cache_dir)
  try:
    from openpilot.selfdrive.modeld.big_model import read_state
    state = read_state(cache_dir)
  except Exception:
    return value

  changed = False
  for key in ('active', 'previous'):
    manifest = state.get(key)
    if manifest is None:
      continue
    if manifest.sha256 not in value['models']:
      value['models'][manifest.sha256] = {
        'model_id': manifest.model_id,
        'sha256': manifest.sha256,
        'size': manifest.size,
        'url': manifest.url,
        'filename': manifest.filename,
      }
      changed = True
  if value.get('selected') is None and state.get('active') is not None:
    value['selected'] = state['active'].sha256
    changed = True
  if changed:
    write_index(value, cache_dir)
  return value


def _downloaded_sha_prefixes(cache_dir: Path | None = None) -> set[str]:
  """First 16 hex chars of every delivered model file present on disk."""
  prefixes = set()
  directory = _cache_dir(cache_dir)
  try:
    for pattern in MODEL_GLOBS:
      for path in directory.glob(pattern):
        stem = path.stem  # big_driving_supercombo-<sha16> / big_driving_tinygrad-<sha16>
        prefix = stem.rsplit('-', 1)[-1]
        if len(prefix) == 16:
          prefixes.add(prefix.lower())
  except OSError:
    pass
  return prefixes


def optional_manifests() -> list:
  """Built-in checkpoints this branch offers but does not download by default.

  The index only knows a model once a server catalog advertised it or a download
  finished, so without this a device could never opt into an extra checkpoint
  before the server listed it. The dicts themselves live in model_source
  (fork-only), and a bad entry is skipped there rather than raised here.
  """
  from openpilot.selfdrive.modeld import model_source
  return model_source.optional_manifests()


def list_models(cache_dir: Path | None = None) -> list[dict]:
  """Every known model, annotated with whether it is downloaded and selected."""
  value = _sync_from_state(cache_dir)
  present = _downloaded_sha_prefixes(cache_dir)
  selected = value.get('selected')
  out = []
  for sha, meta in sorted(value['models'].items(), key=lambda kv: kv[1].get('model_id') or ''):
    item = dict(meta)
    item['sha256'] = sha
    item['downloaded'] = sha[:16].lower() in present
    item['selected'] = sha == selected
    out.append(item)
  # Built-in opt-in checkpoints (Cinque v3) the index has never seen yet.
  seen = {item['sha256'] for item in out}
  for manifest in optional_manifests():
    if manifest.sha256 in seen:
      continue
    out.append({
      'model_id': manifest.model_id,
      'sha256': manifest.sha256,
      'size': manifest.size,
      'url': manifest.url,
      'filename': manifest.filename,
      'downloaded': manifest.sha256[:16].lower() in present,
      'selected': manifest.sha256 == selected,
      'builtin': True,
    })
  return out


def selected_sha(cache_dir: Path | None = None) -> str | None:
  return _sync_from_state(cache_dir).get('selected')


def select(sha: str | None, cache_dir: Path | None = None) -> bool:
  """Remember the user's pick. Unknown hashes are rejected."""
  value = _sync_from_state(cache_dir)
  if sha is None:
    value['selected'] = None
    write_index(value, cache_dir)
    return True
  if sha not in value['models']:
    # A built-in optional checkpoint (Cinque v3) is known to this branch even though
    # the index has never seen it; register it on the first pick so the downloader
    # has a URL and filename to target.
    for manifest in optional_manifests():
      if manifest.sha256 == sha:
        value['models'][sha] = {
          'model_id': manifest.model_id,
          'sha256': manifest.sha256,
          'size': manifest.size,
          'url': manifest.url,
          'filename': manifest.filename,
        }
        break
    else:
      return False
  value['selected'] = sha
  write_index(value, cache_dir)
  return True


def selected_manifest(cache_dir: Path | None = None):
  """The manifest the downloader should target, or None to use the pinned one.

  Returning None keeps upstream behaviour intact (the built-in Cinque v2 pin)
  whenever the user has not chosen anything.
  """
  sha = selected_sha(cache_dir)
  if sha is None:
    return None
  meta = _sync_from_state(cache_dir)['models'].get(sha)
  if meta is None:
    return None
  try:
    from openpilot.selfdrive.modeld.big_model import BigModelManifest
    return BigModelManifest.from_dict(meta, meta.get('url', ''))
  except Exception:
    return None
