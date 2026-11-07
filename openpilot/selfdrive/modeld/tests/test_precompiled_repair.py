import json
from pathlib import Path

from openpilot.selfdrive.modeld import precompiled_repair as R

SHA = 'b4dead3ee091d66ab002a6b8e716c26f4f79e3c931e4be5f48e017ba92f6e960'
RUNTIME = 'runtime-96c6ecc92fe50850'


def install(tmp_path, *, torn=False, marker=False, rejected=True, extra=()):
  root = tmp_path / 'precompiled' / SHA
  runtime = root / RUNTIME
  (runtime / 'tinygrad').mkdir(parents=True)
  (runtime / 'tinygrad' / '__init__.py').write_text('')
  (runtime / 'tinygrad' / 'tensor.py').write_text('' if torn else 'class Tensor:\n  pass\n')
  for name in extra:
    (runtime / name).write_text('')
  if marker:
    (runtime / R.MARKER).write_text('ok\n')
  (root / 'installed.json').write_text(json.dumps({'runtime_directory': RUNTIME}))
  if rejected:
    (root / 'rejected').write_text(SHA)
    (root / 'last_failure.json').write_text('{}')
  return root, runtime


def test_torn_runtime_is_rebuilt(tmp_path):
  root, runtime = install(tmp_path, torn=True)
  assert R.runtime_problem(runtime)

  actions = R.repair_installed(tmp_path)

  assert actions and 'empty tinygrad/tensor.py' in actions[0]
  assert not runtime.exists()
  assert not (root / 'installed.json').exists()
  assert not (root / 'rejected').exists()
  assert not (root / 'last_failure.json').exists()


def test_complete_runtime_is_kept(tmp_path):
  root, runtime = install(tmp_path)
  assert R.runtime_problem(runtime) is None

  assert R.repair_installed(tmp_path) == []
  assert runtime.is_dir()
  assert (root / 'rejected').is_file()


def test_marker_means_complete(tmp_path):
  _, runtime = install(tmp_path, marker=True)
  assert R.runtime_problem(runtime) is None


def test_check_mode_reports_without_touching(tmp_path):
  root, runtime = install(tmp_path, torn=True)
  actions = R.repair_installed(tmp_path, check=True)

  assert actions and 'empty tinygrad/tensor.py' in actions[0]
  assert runtime.is_dir()
  assert (root / 'installed.json').is_file()


def test_empty_package_markers_are_not_damage(tmp_path):
  _, runtime = install(tmp_path, extra=('tinygrad/py.typed',))
  assert R.runtime_problem(runtime) is None


def test_flush_installed_writes_marker(tmp_path):
  _, runtime = install(tmp_path)

  assert R.flush_installed(runtime.parent / 'model.pkl') is True
  assert (runtime / R.MARKER).is_file()


def test_missing_runtime_drops_the_rejection_that_would_block_reinstall(tmp_path):
  root, runtime = install(tmp_path, torn=True)
  runtime.rename(runtime.parent / 'elsewhere')

  actions = R.repair_installed(tmp_path)

  assert actions and 'missing' in actions[0]
  assert not (root / 'installed.json').exists()
  assert not (root / 'rejected').exists()
