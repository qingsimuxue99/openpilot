"""Web API: list, switch between and remove coexisting eGPU big models.

Companion to features/egpu_model.py (which reports the download/compile state of
the *current* model). This one manages the set of models stored side by side in
the model cache dir, so the user can pick one in the web UI.

Routes:
  GET  /api/egpu/models          list (remote catalog merged with local state)
  POST /api/egpu/models/select   choose a model; optional reboot to activate
  POST /api/egpu/models/remove   delete a downloaded model from disk
  POST /api/egpu/models/source   pick the big model delivery (auto/precompiled/chunked)
  POST /api/egpu/models/precompiled/retry  clear a rejected precompiled artifact marker

The payload also carries `source_state`: why the pinned delivery is (or is not) the one in
use, plus whether the precompiled artifact can be retried. The page shows it verbatim - a pin
that cannot be honoured (artifact missing, or rejected by a previous boot) must never look
like the setting was ignored.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from aiohttp import web

from openpilot.common.basedir import BASEDIR
from openpilot.common.file_chunker import get_manifest_path
from openpilot.selfdrive.modeld import chunked_model, helpers, model_catalog
from openpilot.selfdrive.modeld.big_model import active_manifest, model_cache_dir
from openpilot.selfdrive.modeld.model_source import MODEL_BASE, label_for, picker_rank

from ..services.params import HAS_PARAMS, Params

CATALOG_PATH = 'models/catalog.json'
REMOTE_TIMEOUT = 6.0

# The boot launcher starts this downloader (launch_chffrplus.sh: run_big_model_update) and
# looks for the same marker. The page reuses it so picking a model downloads it right away
# instead of waiting for the next boot - where the very first attempt can run before DNS is
# up, and a single unresolvable host used to defer the fetch for a whole boot cycle.
DOWNLOAD_LOG = '/tmp/big_model_update.log'
DOWNLOADER_MARKER = '--ensure-if-egpu'


def _downloader_running() -> bool:
  """Is the background big-model downloader already alive?

  Scans /proc for the same command line marker the launcher and the HUD badge use, so the
  web server never forks a helper process just to answer this.
  """
  try:
    for entry in os.listdir('/proc'):
      if not entry.isdigit():
        continue
      try:
        with open(f'/proc/{entry}/cmdline', 'rb') as handle:
          cmdline = handle.read().decode('utf-8', 'replace')
      except OSError:
        continue
      if 'openpilot.selfdrive.modeld.big_model' in cmdline and DOWNLOADER_MARKER in cmdline:
        return True
  except OSError:
    pass
  return False


def start_model_download() -> bool:
  """Start the background downloader now; True when one is running.

  Picking a model (or a delivery) is the user asking for that artifact, so the page drives
  the download instead of leaving it to the next boot. The downloader decides what is still
  missing, retries transient failures itself and reports through status.json, which the page
  already polls through /api/egpu/model. It is detached and never blocks the request.
  """
  if _downloader_running():
    return True
  try:
    env = dict(os.environ, PYTHONUNBUFFERED='1')
    if not env.get('PYTHONPATH'):
      env['PYTHONPATH'] = os.pathsep.join(path for path in sys.path if path)
    with open(DOWNLOAD_LOG, 'ab') as log:
      subprocess.Popen(
        [sys.executable, '-m', 'openpilot.selfdrive.modeld.big_model',
         DOWNLOADER_MARKER, '--network-wait-seconds', '60'],
        cwd=BASEDIR, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        env=env, start_new_session=True,
      )
    return True
  except Exception:
    return False


def _download_pending(sha: str | None) -> bool:
  """Does the pinned delivery for `sha` still need a transfer?

  Either the model file itself is missing, or the pinned precompiled delivery has no
  installed artifact yet (the ONNX alone does not satisfy that pin).
  """
  if not isinstance(sha, str) or len(sha) != 64:
    return False
  try:
    if not any(path.is_file() for path in _cached_model_files(model_cache_dir(), sha)):
      return True
    return chunked_model.read_model_source() == 'precompiled' and not _precompiled_state(sha)['installed']
  except Exception:
    return False


def _remote_catalog() -> dict[str, Any] | None:
  """Optional server-side list of selectable models."""
  url = urljoin(MODEL_BASE if MODEL_BASE.endswith('/') else MODEL_BASE + '/', CATALOG_PATH)
  try:
    with urlopen(Request(url, headers={'User-Agent': 'carrot-model/1'}), timeout=REMOTE_TIMEOUT) as response:
      if response.status != 200:
        return None
      value = json.loads(response.read().decode('utf-8'))
    return value if isinstance(value, dict) else None
  except Exception:
    return None


def _active_sha() -> str | None:
  try:
    manifest = active_manifest()
    return manifest.sha256 if manifest is not None else None
  except Exception:
    return None


def _modeld_pid() -> int | None:
  try:
    out = subprocess.run(['pgrep', '-f', 'modeld.modeld'], capture_output=True, text=True, timeout=5)
    return int(out.stdout.split()[0])
  except Exception:
    return None


def _running_sha() -> str | None:
  """The model modeld is actually running, or None (internal model / unknown).

  state.json's "active" is what the *next* boot will use: it is rewritten as soon as
  a newly selected model finishes downloading, while modeld keeps running the one it
  loaded at startup. modeld publishes no model id (only UsbGpu* booleans), so derive
  it: if state.json was written after modeld started, what still runs is "previous".
  """
  try:
    from openpilot.common.params import Params
    if not Params().get_bool('UsbGpuActive'):
      return None
  except Exception:
    return None

  try:
    from openpilot.selfdrive.modeld.big_model import read_state, state_path

    state = read_state()
    active, previous = state.get('active'), state.get('previous')
    if active is None:
      return previous.sha256 if previous is not None else None
    pid = _modeld_pid()
    if pid is None or previous is None:
      return active.sha256
    try:
      elapsed = float(subprocess.run(['ps', '-o', 'etimes=', '-p', str(pid)],
                                     capture_output=True, text=True, timeout=5).stdout.strip())
      started = time.time() - elapsed
    except Exception:
      return active.sha256
    return previous.sha256 if started < os.path.getmtime(state_path()) else active.sha256
  except Exception:
    return None


def _chunked_ready(sha: str, precompiled_only: bool) -> bool:
  """A chunked set for `sha` is installed, so nothing is left to compile on the device."""
  if precompiled_only:
    return False  # a generic PKL ships no ONNX to compile and no chunked set to split
  try:
    path = helpers.modeld_pkl_path(usbgpu=True, model_sha256=sha)
    return Path(get_manifest_path(path)).is_file()
  except Exception:
    return False


def _precompiled_state(sha: str) -> dict[str, Any]:
  """On-disk state of the precompiled artifact for `sha` (installed / rejected / last failure)."""
  root = model_cache_dir() / 'precompiled' / sha
  state: dict[str, Any] = {
    'installed': (root / 'installed.json').is_file() and (root / 'model.pkl').is_file(),
    'rejected': (root / 'rejected').is_file(),
  }
  if state['rejected']:
    try:
      state['rejected_at'] = int((root / 'rejected').stat().st_mtime)
    except OSError:
      pass
  failure = root / 'last_failure.json'
  if failure.is_file():
    try:
      value = json.loads(failure.read_text(encoding='utf-8'))
      state['last_failure'] = {'phase': value.get('phase'), 'time': value.get('time'),
                               'permanent': bool(value.get('rejected')),
                               'error': str(value.get('error', ''))[:300]}
    except (OSError, ValueError):
      pass
  return state


def _delivery_state(sha: str, precompiled_only: bool) -> dict[str, Any]:
  return {'chunked_ready': _chunked_ready(sha, precompiled_only), 'precompiled': _precompiled_state(sha)}


def _source_state(source: str, delivery: str | None, active_sha: str | None) -> dict[str, Any]:
  """Why `delivery` is (not) the pinned `source`, in words the page shows verbatim."""
  if active_sha is None:
    return {'note': '', 'retry_available': False}
  info = _precompiled_state(active_sha)
  retry = bool(info.get('rejected')) or (source == 'precompiled' and not info.get('installed'))
  if source == 'precompiled' and delivery != 'precompiled':
    if info.get('rejected'):
      note = '预编译包已被本机拒绝，「清除拒绝并重试」后重启可重新安装'
    elif not info.get('installed'):
      note = '预编译包尚未安装，重启后会自动下载'
    else:
      note = '预编译包已就绪，重启后生效'
    return {'note': note, 'retry_available': retry}
  if source == 'chunked' and delivery == 'precompiled':
    return {'note': '该型号没有分片包，只能按预编译包运行', 'retry_available': False}
  if source == 'chunked' and delivery is None:
    return {'note': '分片包尚未准备好，当前走本机编译', 'retry_available': False}
  return {'note': '', 'retry_available': retry}


def _entry(label: str | None, model_id: str | None, sha: str, size, url, *, downloaded: bool,
           selected: bool, is_active: bool, precompiled_only: bool = False) -> dict[str, Any]:
  return {
    'label': label or model_id or sha[:12],
    'model_id': model_id,
    'sha256': sha,
    'size': size,
    'url': url,
    'downloaded': downloaded,
    'selected': selected,
    'active': is_active,
    # The page uses this to explain that the delivery setting cannot apply to such a model.
    'precompiled_only': precompiled_only,
    # What each delivery can offer for this model right now (installed / rejected / missing).
    'delivery_state': _delivery_state(sha, precompiled_only),
  }


def _is_precompiled_only(filename) -> bool:
  """True for models delivered as an already-compiled PKL (Cinque v3).

  Mirrors BigModelManifest.precompiled_only: the file *is* the model, so there is no ONNX
  to compile locally and no chunked set, and the precompiled artifact is the only delivery
  that can produce it.
  """
  return isinstance(filename, str) and filename.endswith('.pkl')


def _cached_model_files(root: Path, sha: str) -> list[Path]:
  """Every delivery file this model may occupy in the cache dir.

  The name follows the manifest (`<stem>-<sha16><suffix>`, the same rule as
  BigModelManifest.cache_filename), which keeps the generic precompiled PKL deletable too:
  the ONNX name was hardcoded here, so removing Cinque v3 left its 776 MB on disk and the
  page kept reporting it as downloaded. The legacy ONNX name stays in the list for the
  pinned model, which predates the manifest-derived one.
  """
  names = {f'big_driving_supercombo-{sha[:16]}.onnx'}
  for meta in model_catalog.list_models():
    filename = meta.get('filename')
    if meta.get('sha256') == sha and isinstance(filename, str) and filename:
      stem, suffix = os.path.splitext(filename)
      names.add(f'{stem}-{sha[:16]}{suffix}')
  return [root / name for name in sorted(names)]


def build_models_payload() -> dict[str, Any]:
  local = model_catalog.list_models()
  by_sha = {item['sha256']: item for item in local}
  selected = model_catalog.selected_sha()
  # "active" is what the next boot will load; "running" is what modeld loaded this
  # boot. They differ right after a switch finished downloading (until the reboot).
  active = _active_sha()
  running = _running_sha()

  models: list[dict[str, Any]] = []
  remote = _remote_catalog()
  for entry in (remote or {}).get('models', []) or []:
    if not isinstance(entry, dict):
      continue
    sha = entry.get('sha256')
    if not isinstance(sha, str) or len(sha) != 64:
      continue
    known = by_sha.pop(sha, {})
    # known comes from list_models(), which decides `downloaded` from the ONNX files
    # actually on disk. The index also holds entries pre-registered from the remote
    # catalog, so its presence alone must not count as "downloaded".
    # The server's label wins; without one (or offline) the branch's own short name is
    # used so the picker never shows a raw comma- id for a model we ship.
    models.append(_entry(entry.get('label') or label_for(sha, entry.get('model_id')),
                         entry.get('model_id'), sha, entry.get('size'),
                         entry.get('url'), downloaded=bool(known.get('downloaded')),
                         selected=sha == selected, is_active=sha == running,
                         precompiled_only=_is_precompiled_only(entry.get('filename')
                                                               or known.get('filename'))))

  # Anything on disk that the server no longer lists still has to be switchable, and so
  # do the built-in opt-in checkpoints (Cinque v3) that no catalog advertises yet - for
  # those `downloaded` must come from the index instead of being assumed true.
  for sha, known in by_sha.items():
    models.append(_entry(label_for(sha, known.get('model_id')), known.get('model_id'), sha,
                         known.get('size'), known.get('url'),
                         downloaded=bool(known.get('downloaded')), selected=sha == selected,
                         is_active=sha == running,
                         precompiled_only=_is_precompiled_only(known.get('filename'))))

  # The branch's own model (Cinque v3 = "CTM V3") leads the picker. Sorting is stable,
  # so the server catalog's order is kept for everything else.
  models.sort(key=lambda model: picker_rank(model.get('sha256')))

  source = chunked_model.read_model_source()
  delivery = chunked_model.active_delivery()
  return {
    'ok': True,
    'selected': selected,
    'active': active,
    'running': running,
    'source': source,
    'sources': list(chunked_model.SOURCES),
    'delivery': delivery,
    # Why the pinned delivery is not the one in use, so the page can say it out loud.
    'source_state': _source_state(source, delivery, active),
    'remote_catalog': remote is not None,
    'models': models,
  }


async def api_models(_request: web.Request) -> web.Response:
  return web.json_response(build_models_payload())


async def api_select(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({'ok': False, 'error': 'invalid JSON body'}, status=400)
  if not isinstance(body, dict):
    return web.json_response({'ok': False, 'error': 'invalid body'}, status=400)

  sha = body.get('sha256')
  if not isinstance(sha, str) or len(sha) != 64:
    return web.json_response({'ok': False, 'error': 'sha256 required'}, status=400)

  # The device only knows models it has already downloaded (register() runs after
  # a successful fetch). Record the server's entry first, so a model that is
  # listed but not yet present can be picked, and so picking one refreshes its
  # URL - that is how a server-side layout change reaches devices still holding
  # the old address.
  for entry in (_remote_catalog() or {}).get('models', []) or []:
    if isinstance(entry, dict) and entry.get('sha256') == sha:
      model_catalog.register_remote(entry)
      break

  if not model_catalog.select(sha):
    return web.json_response({'ok': False, 'error': 'unknown model'}, status=404)

  # Drive the download from the picker itself: the boot downloader's first attempt can run
  # before DNS is up, and a deferred fetch stayed invisible until the next boot.
  pending = _download_pending(sha)
  started = start_model_download() if pending else False
  # Rebooting while the artifact is still missing would only abort the transfer, so it waits
  # for the download; the page shows the progress and offers the restart afterwards.
  reboot = bool(body.get('reboot')) and not pending
  if reboot and HAS_PARAMS and Params is not None:
    try:
      Params().put_bool('DoReboot', True)
    except Exception:
      reboot = False
  return web.json_response({'ok': True, 'selected': sha, 'reboot_requested': reboot,
                            'download_started': started, 'download_pending': pending})


async def api_remove(request: web.Request) -> web.Response:
  try:
    body = await request.json()
  except Exception:
    return web.json_response({'ok': False, 'error': 'invalid JSON body'}, status=400)
  sha = (body or {}).get('sha256') if isinstance(body, dict) else None
  if not isinstance(sha, str) or len(sha) != 64:
    return web.json_response({'ok': False, 'error': 'sha256 required'}, status=400)

  if HAS_PARAMS and Params is not None:
    try:
      if Params().get_bool('IsEngaged'):
        return web.json_response({'ok': False, 'error': 'disengage openpilot before removing a model'},
                                 status=409)
    except Exception:
      pass
  if sha == _active_sha():
    return web.json_response({'ok': False, 'error': 'cannot remove the model currently in use'},
                             status=409)
  # state.json's active is what the *next* boot loads. modeld can still be running a
  # different one (a switch finished downloading but was not rebooted yet), and the
  # web page hides the delete button for it - check it here too so the API cannot be
  # used to pull the files out from under the running model.
  if sha == _running_sha():
    return web.json_response({'ok': False, 'error': 'cannot remove the model modeld is running'},
                             status=409)
  if sha == model_catalog.selected_sha():
    return web.json_response({'ok': False, 'error': 'cannot remove the selected model'},
                             status=409)

  root = model_cache_dir()
  removed = []
  for path in _cached_model_files(root, sha):
    try:
      if path.is_file():
        path.unlink()
        removed.append(path.name)
    except OSError as exc:
      return web.json_response({'ok': False, 'error': str(exc)}, status=500)
  precompiled = root / 'precompiled' / sha
  try:
    if precompiled.is_dir():
      shutil.rmtree(precompiled)
      removed.append(str(precompiled.relative_to(root)))
  except OSError as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=500)

  # Chunked artifacts live in the repository model dir (the local compiler's output).
  from openpilot.selfdrive.modeld.chunked_model import remove_chunked_sha

  try:
    removed += remove_chunked_sha(sha)
  except OSError as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=500)

  value = model_catalog.read_index()
  if value['models'].pop(sha, None) is not None:
    model_catalog.write_index(value)
  return web.json_response({'ok': True, 'removed': removed})


async def api_source(request: web.Request) -> web.Response:
  """Pick how the big model is delivered (see chunked_model.read_model_source)."""
  try:
    body = await request.json()
  except Exception:
    return web.json_response({'ok': False, 'error': 'invalid JSON body'}, status=400)
  source = (body.get('source') if isinstance(body, dict) else None)
  try:
    chunked_model.write_model_source(source)
  except ValueError as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=400)
  except OSError as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=500)
  # Changing the delivery is the same promise as picking a model: fetch whatever that pin
  # still needs (a pinned precompiled package, typically) without waiting for a reboot.
  pending = _download_pending(model_catalog.selected_sha())
  started = start_model_download() if pending else False
  return web.json_response({'ok': True, 'source': source,
                            'download_started': started, 'download_pending': pending})


async def api_precompiled_retry(request: web.Request) -> web.Response:
  """Clear the marker that stops a rejected precompiled artifact from ever being retried.

  precompiled_model.reject() latches a permanent boot-validation failure into
  precompiled/<sha>/rejected, and ensure_precompiled() then skips that artifact for as long as
  the marker matches the served pickle. Clearing it is the only way back to the precompiled
  delivery for that model, so this also starts the download right away (the artifact is
  usually still missing at this point) and asks for a restart only once it is installed -
  modeld resolves the artifact once, at startup.
  """
  try:
    body = await request.json()
  except Exception:
    body = {}
  sha = (body or {}).get('sha256') if isinstance(body, dict) else None
  if not isinstance(sha, str) or len(sha) != 64:
    sha = _active_sha()
  if not isinstance(sha, str):
    return web.json_response({'ok': False, 'error': 'no model selected'}, status=409)

  marker = model_cache_dir() / 'precompiled' / sha / 'rejected'
  try:
    cleared = marker.is_file()
    marker.unlink(missing_ok=True)
  except OSError as exc:
    return web.json_response({'ok': False, 'error': str(exc)}, status=500)
  started = start_model_download()
  installed = bool(_precompiled_state(sha)['installed'])
  return web.json_response({'ok': True, 'sha256': sha, 'cleared': cleared,
                            'download_started': started, 'reboot_required': installed})


def register(app: web.Application) -> None:
  app.router.add_get('/api/egpu/models', api_models)
  app.router.add_post('/api/egpu/models/select', api_select)
  app.router.add_post('/api/egpu/models/remove', api_remove)
  app.router.add_post('/api/egpu/models/source', api_source)
  app.router.add_post('/api/egpu/models/precompiled/retry', api_precompiled_retry)
