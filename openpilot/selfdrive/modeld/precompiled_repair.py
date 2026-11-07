"""Recover a precompiled eGPU runtime that a power cut left torn on disk.

`precompiled_model.ensure_precompiled()` extracts `runtime.tar.gz` into
`precompiled/<sha>/runtime-<prefix>/` and only asks whether that directory exists.
The downloads are fsynced, the extracted files are not, so pulling the power right
after the rename leaves a runtime that looks installed but holds zero-byte files -
the worker then dies on `from tinygrad import Tensor` and the model never runs.

Nothing can retry that on its own: the directory exists, so every later boot reuses
it, and normal users have no shell to delete it. This module is fork-only, so
`precompiled_model.py` stays upstream's file: it is called from two fork entry
points instead, `big_model_catalog.__main__` (once per boot, before build/modeld)
and `precompiled_fetch.fetch_after_onnx` (the downloader).

Repair stays conservative: a runtime is discarded only when a file that must have
content is empty or the entry point is gone. A model that genuinely does not run on
this hardware keeps its rejection instead of being retried on every boot.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

# Written last by mark_and_flush(): its presence means every file below is on disk.
MARKER = '.extracted'
# Files that are legitimately empty in a tinygrad tree.
EMPTY_OK = frozenset({'__init__.py', 'py.typed'})
# At least one of these has to exist and has to have content.
ENTRY_FILES = ('tinygrad/tensor.py', 'tinygrad/__init__.py', 'model_runtime.py')
# Written by the install; all of them describe a runtime we just threw away.
INSTALL_MARKERS = ('installed.json', 'rejected', 'boot_validation.json', 'last_failure.json')


def cache_root(cache_dir=None) -> Path:
  """Model cache dir, resolved the same way big_model_catalog.storage() resolves it.

  Duplicated on purpose: importing that module here would make the two modules
  circular (its __main__ calls repair_installed()).
  """
  if cache_dir is not None:
    return Path(cache_dir)
  override = os.getenv('CARROT_BIG_MODEL_DIR')
  if override:
    return Path(override)
  return Path('/data/media/0/carrot/models') if Path('/TICI').is_file() else Path.home() / '.comma/models'


def runtime_problem(runtime: Path) -> str | None:
  """Why this extracted runtime cannot be trusted, or None when it looks usable."""
  if not runtime.is_dir():
    return 'runtime directory is missing'
  if (runtime / MARKER).is_file():
    return None
  if not any((runtime / name).is_file() for name in ENTRY_FILES):
    return 'no runtime entry point'
  try:
    for path in runtime.rglob('*'):
      if not path.is_file() or path.name in EMPTY_OK:
        continue
      if path.stat().st_size == 0:
        return f'empty {path.relative_to(runtime).as_posix()}'
  except OSError as exc:
    return f'unreadable runtime ({exc})'
  return None


def _fsync(path: Path) -> None:
  try:
    fd = os.open(path, os.O_RDONLY)
  except OSError:
    return  # directories cannot be opened this way on every platform
  try:
    os.fsync(fd)
  except OSError:
    pass
  finally:
    os.close(fd)


def _log(message: str) -> None:
  try:
    from openpilot.common import swaglog
    swaglog.cloudlog.warning(message)
    return
  except Exception:
    pass
  try:
    print(message, file=sys.stderr)
  except Exception:
    pass


def installed_runtime(model_path) -> Path | None:
  """The runtime directory `installed.json` points at, next to the model pickle."""
  try:
    value = json.loads((Path(model_path).parent / 'installed.json').read_text(encoding='utf-8'))
  except (OSError, ValueError):
    return None
  name = value.get('runtime_directory')
  if not isinstance(name, str) or not name or '/' in name or os.sep in name:
    return None
  return Path(model_path).parent / name


def mark_and_flush(runtime: Path) -> bool:
  """Persist every extracted file, then claim the directory with a marker.

  The marker goes last: a directory without one is treated as "not known to be
  complete" by runtime_problem(), so an interrupted flush is repaired later instead
  of being trusted forever.
  """
  if not runtime.is_dir():
    return False
  try:
    for path in runtime.rglob('*'):
      if path.is_file():
        _fsync(path)
    (runtime / MARKER).write_text('ok\n', encoding='utf-8')
    _fsync(runtime / MARKER)
    _fsync(runtime)
    return True
  except OSError:
    return False


def flush_installed(model_path) -> bool:
  """Flush the runtime installed for `model_path` (a pickle next to installed.json)."""
  runtime = installed_runtime(model_path)
  return bool(runtime and mark_and_flush(runtime))


def repair_installed(cache_dir=None, *, check: bool = False) -> list[str]:
  """Discard torn runtimes so the verified tarball gets extracted again.

  Deleting `installed.json` and the rejection is what makes the next
  `ensure_precompiled()` re-extract from the local `runtime.tar.gz` (already hash
  checked on download) instead of short-circuiting on the broken directory.
  """
  root = cache_root(cache_dir) / 'precompiled'
  actions: list[str] = []
  try:
    entries = sorted(root.glob('*/installed.json'))
  except OSError:
    return actions
  for entry in entries:
    try:
      value = json.loads(entry.read_text(encoding='utf-8'))
    except (OSError, ValueError):
      continue
    name = value.get('runtime_directory')
    if not isinstance(name, str) or not name or '/' in name or os.sep in name:
      continue
    runtime = entry.parent / name
    problem = runtime_problem(runtime)
    if problem is None:
      continue
    model = entry.parent.name[:12]
    if check:
      actions.append(f'{model}: {problem}')
      continue
    shutil.rmtree(runtime, ignore_errors=True)
    for marker in INSTALL_MARKERS:
      (entry.parent / marker).unlink(missing_ok=True)
    action = f'rebuilding precompiled runtime {model}: {problem}'
    _log(action)
    actions.append(action)
  return actions


if __name__ == '__main__':
  import argparse

  parser = argparse.ArgumentParser(description='Repair torn precompiled eGPU runtimes')
  parser.add_argument('--check', action='store_true', help='report only, change nothing')
  parser.add_argument('--cache-dir', type=Path, default=None)
  args = parser.parse_args()
  for line in repair_installed(args.cache_dir, check=args.check):
    print(line)
