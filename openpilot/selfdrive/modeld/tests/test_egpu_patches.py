"""Guards for the fork's eGPU runtime patches (see modeld/egpu_patches.py).

These tests keep two promises:

* the patches work the way the eGPU stack needs (env default, firmware search order,
  USB link failures staying retryable, the sitecustomize hook actually firing);
* the patches stay OUT of upstream files - the whole point of egpu_patches.py is that
  merging carrot-wip cannot conflict on them. If a patch is ever added back to one of
  those files, the last test fails and points at the right place.
"""
import os
import subprocess
import sys
import types
from hashlib import sha256
from pathlib import Path

import pytest

from openpilot.common.basedir import BASEDIR
from openpilot.selfdrive.modeld import egpu_patches


@pytest.fixture(autouse=True)
def _no_global_env_leak(monkeypatch):
  monkeypatch.delenv("AM_POWER_LIMIT", raising=False)
  yield
  monkeypatch.delenv("AM_POWER_LIMIT", raising=False)


def _stub_helpers():
  module = types.ModuleType("stub_tinygrad_helpers")
  module.fetch_fw = lambda path, name, sha256_hex: b"original"
  return module


def test_the_chunked_model_runs_uncapped_by_default(monkeypatch):
  # The chunked path runs through this repo's tinygrad, which retries USB transfers and has
  # run uncapped for hours, so no cap is injected for it.
  assert egpu_patches.AM_POWER_LIMIT == ""
  assert egpu_patches.apply_env_defaults() is False
  assert "AM_POWER_LIMIT" not in os.environ
  # Already set by the environment: a later call must not change it back.
  monkeypatch.setenv("AM_POWER_LIMIT", "90")
  assert egpu_patches.apply_env_defaults() is False
  assert os.environ["AM_POWER_LIMIT"] == "90"


def test_power_limit_file_governs_only_the_precompiled_worker(monkeypatch, tmp_path):
  ctl = tmp_path / "egpu_power_limit"
  monkeypatch.setattr(egpu_patches, "EGPU_POWER_LIMIT_FILE", str(ctl))
  monkeypatch.delenv("AM_POWER_LIMIT_PRECOMPILED", raising=False)
  monkeypatch.setattr(sys, "argv", ["/data/openpilot/openpilot/selfdrive/modeld/precompiled_worker.py", "model.pkl"])

  # No file: the built-in chestnut wall.
  assert egpu_patches.file_power_limit() is None
  assert egpu_patches.apply_env_defaults() is True
  assert os.environ["AM_POWER_LIMIT"] == egpu_patches.AM_POWER_LIMIT_PRECOMPILED == "120"

  # Empty file: uncapped on purpose.
  ctl.write_text("")
  assert egpu_patches.file_power_limit() == ""
  assert egpu_patches.apply_env_defaults() is True
  assert "AM_POWER_LIMIT" not in os.environ

  # A number: that many watts, however it is written.
  for text, expected in (("150", "150"), ("150W\n", "150"), ("150.0", "150"), (" 90 w ", "90")):
    ctl.write_text(text)
    assert egpu_patches.file_power_limit() == expected
    assert egpu_patches.apply_env_defaults() is True
    assert os.environ["AM_POWER_LIMIT"] == expected

  # Unparsable content keeps the default instead of uncapping the GPU by accident.
  ctl.write_text("as fast as possible")
  assert egpu_patches.file_power_limit() is None

  # The same file must never cap the chunked model.
  monkeypatch.setattr(sys, "argv", ["/data/openpilot/openpilot/selfdrive/modeld/modeld.py"])
  monkeypatch.delenv("AM_POWER_LIMIT", raising=False)
  assert egpu_patches.apply_env_defaults() is False
  assert "AM_POWER_LIMIT" not in os.environ


def test_fetch_fw_searches_local_dirs_before_the_original(monkeypatch, tmp_path):
  blob = b"firmware blob"
  firmware_dir = tmp_path / "amdgpu"
  firmware_dir.mkdir()
  (firmware_dir / "smu_14_0_2.bin.zst").write_bytes(blob)
  monkeypatch.setattr(egpu_patches, "firmware_bases", lambda: (str(tmp_path),))
  monkeypatch.setattr(egpu_patches, "_decompress", lambda raw: raw)  # no zstandard needed

  helpers = _stub_helpers()
  assert egpu_patches.patch_fetch_fw(helpers) is True
  # A local blob with the pinned hash wins, even though the original would have run.
  assert helpers.fetch_fw("amdgpu", "smu_14_0_2.bin", sha256(blob).hexdigest()) == blob
  # A miss (unknown hash, unknown name) falls through to tinygrad's own logic.
  assert helpers.fetch_fw("amdgpu", "smu_14_0_2.bin", "0" * 64) == b"original"
  assert helpers.fetch_fw("amdgpu", "other.bin", sha256(blob).hexdigest()) == b"original"
  # Idempotent: a second pass is a no-op, so double-patching cannot nest wrappers.
  assert egpu_patches.patch_fetch_fw(helpers) is False


def test_usb_link_failures_stay_retryable(monkeypatch):
  seen: list[tuple[str, object]] = []
  module = types.ModuleType("stub_precompiled_model")
  module.record_failure = lambda path, error, phase: (seen.append((str(error), phase)), True)[1]
  assert egpu_patches.patch_record_failure(module) is True

  module.record_failure("p", RuntimeError("bulk IN transfer failed"), "inference")
  detail, phase = seen[-1]
  assert "precompiled eGPU worker timed out" in detail  # the wording upstream treats as transient
  assert "bulk IN transfer failed" in detail            # while keeping the real reason
  assert phase == "inference"

  module.record_failure("p", RuntimeError("Failed to acquire lock file am_usb:4-2.lock"), "load")
  assert "precompiled eGPU worker timed out" in seen[-1][0]

  # Unrelated failures must keep their meaning: a broken artifact stays rejected.
  module.record_failure("p", RuntimeError("non-finite model output"), "inference")
  assert "precompiled eGPU worker timed out" not in seen[-1][0]
  assert egpu_patches.patch_record_failure(module) is False


def test_a_pinned_precompiled_delivery_is_not_substituted(monkeypatch, tmp_path):
  """Pinning the precompiled package must not resolve to the chunked set on disk.

  Upstream's resolution is "precompiled first, chunked second", so a pin whose artifact was
  missing (or rejected) used to be answered with the chunked artifact: the page said one
  delivery while the car drove the other. This patch is what makes the answer reflect the pin.
  """
  from openpilot.selfdrive.modeld import big_model, chunked_model, helpers, precompiled_model

  model = types.SimpleNamespace(sha256="a" * 64, precompiled_only=False)
  monkeypatch.setattr(big_model, "selected_manifest", lambda: model)
  # Restore whatever the module held before this test, patch or not.
  monkeypatch.setattr(helpers, "active_usbgpu_compiled_path", helpers.active_usbgpu_compiled_path)
  monkeypatch.setattr(helpers, "usbgpu_compile_pending", helpers.usbgpu_compile_pending)
  monkeypatch.delattr(helpers, "_fork_model_delivery", raising=False)
  assert egpu_patches.patch_model_delivery(helpers) is True
  assert egpu_patches.patch_model_delivery(helpers) is False  # idempotent

  chunked_pkl = tmp_path / "big_driving_supercombo.pkl"
  manifest_path = Path(helpers.get_manifest_path(chunked_pkl))
  manifest_path.parent.mkdir(parents=True, exist_ok=True)
  manifest_path.write_text("1")  # a usable chunked set is on disk
  monkeypatch.setattr(helpers, "modeld_pkl_path", lambda **_kwargs: chunked_pkl)
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_args, **_kwargs: "precompiled")
  monkeypatch.setattr(precompiled_model, "installed", lambda *_args, **_kwargs: None)

  # Pinned precompiled and not installed: reported as nothing to load, and as pending - not
  # as the chunked artifact that happens to be installed.
  assert helpers.active_usbgpu_compiled_path() is None
  assert helpers.usbgpu_compile_pending()

  precompiled_path = tmp_path / "precompiled" / "model.pkl"
  monkeypatch.setattr(precompiled_model, "installed", lambda *_args, **_kwargs: precompiled_path)
  assert helpers.active_usbgpu_compiled_path() == precompiled_path
  assert not helpers.usbgpu_compile_pending()

  # Every other pin keeps upstream's own logic, which chunked_model's `rejected` marker
  # steers towards the chunked artifact.
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_args, **_kwargs: "auto")
  monkeypatch.setattr(precompiled_model, "installed", lambda *_args, **_kwargs: None)
  assert helpers.active_usbgpu_compiled_path() == chunked_pkl


def test_a_pinned_chunked_delivery_beats_an_installed_precompiled_package(monkeypatch, tmp_path):
  """Pinning the chunked set must win over an installed precompiled package.

  Upstream resolves "precompiled first, chunked second", and the precompiled `rejected`
  marker that would steer it towards the chunked artifact only appears after a failed boot -
  nothing creates it for a chunked pin. So the pin used to be ignored whenever a precompiled
  package happened to be installed, which is the common case after a first download.
  """
  from openpilot.selfdrive.modeld import big_model, chunked_model, helpers, precompiled_model

  monkeypatch.setattr(helpers, "active_usbgpu_compiled_path", helpers.active_usbgpu_compiled_path)
  monkeypatch.setattr(helpers, "usbgpu_compile_pending", helpers.usbgpu_compile_pending)
  monkeypatch.delattr(helpers, "_fork_model_delivery", raising=False)
  assert egpu_patches.patch_model_delivery(helpers) is True
  assert egpu_patches.patch_model_delivery(helpers) is False  # idempotent

  model = types.SimpleNamespace(sha256="b" * 64, precompiled_only=False)
  monkeypatch.setattr(big_model, "selected_manifest", lambda: model)
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_args, **_kwargs: "chunked")

  precompiled_pkl = tmp_path / "precompiled" / "model.pkl"
  monkeypatch.setattr(precompiled_model, "installed", lambda *_args, **_kwargs: precompiled_pkl)
  chunked_pkl = tmp_path / "big_driving_tinygrad.pkl"
  manifest = Path(helpers.get_manifest_path(chunked_pkl))
  manifest.parent.mkdir(parents=True, exist_ok=True)
  manifest.write_text("1")
  monkeypatch.setattr(helpers, "modeld_pkl_path", lambda **_kwargs: chunked_pkl)

  # Chunked set installed: the pin beats the installed precompiled package.
  assert helpers.active_usbgpu_compiled_path() == chunked_pkl
  assert not helpers.usbgpu_compile_pending()

  # Pinned but not built yet: pending, and never the precompiled package.
  missing = tmp_path / "missing.pkl"
  monkeypatch.setattr(helpers, "modeld_pkl_path", lambda **_kwargs: missing)
  assert helpers.active_usbgpu_compiled_path() is None
  assert helpers.usbgpu_compile_pending()

  # auto prefers the chunked set as well, and falls back to precompiled when it is gone.
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_args, **_kwargs: "auto")
  monkeypatch.setattr(helpers, "modeld_pkl_path", lambda **_kwargs: chunked_pkl)
  assert helpers.active_usbgpu_compiled_path() == chunked_pkl
  monkeypatch.setattr(helpers, "modeld_pkl_path", lambda **_kwargs: missing)
  assert helpers.active_usbgpu_compiled_path() == precompiled_pkl

  # A precompiled-only model has no chunked set, so every pin keeps upstream's answer.
  only_pkl = types.SimpleNamespace(sha256="c" * 64, precompiled_only=True)
  monkeypatch.setattr(big_model, "selected_manifest", lambda: only_pkl)
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_args, **_kwargs: "chunked")
  assert helpers.active_usbgpu_compiled_path() == precompiled_pkl


def test_patch_after_import_runs_once_at_import(monkeypatch, tmp_path):
  (tmp_path / "stub_late_module.py").write_text("VALUE = 41\n")
  monkeypatch.syspath_prepend(str(tmp_path))
  fired: list[int] = []
  assert egpu_patches.patch_after_import("stub_late_module", lambda module: fired.append(module.VALUE) or True) is True
  assert fired == []
  import stub_late_module  # noqa: PLC0415 - imported on purpose after the hook is armed

  assert stub_late_module.VALUE == 41
  assert fired == [41]


def test_patch_after_import_handles_an_already_imported_module():
  module = types.ModuleType("stub_preloaded_module")
  module.mark = "pre"
  sys.modules["stub_preloaded_module"] = module
  try:
    fired: list[str] = []
    assert egpu_patches.patch_after_import("stub_preloaded_module", lambda m: fired.append(m.mark) or True) is True
    assert fired == ["pre"]
  finally:
    sys.modules.pop("stub_preloaded_module", None)


@pytest.mark.skipif(os.environ.get("OPENPILOT_EGPU_PATCH") == "0", reason="patches disabled by the environment")
def test_sitecustomize_applies_the_patches_in_a_new_process():
  """The hook has to fire without anyone importing it - that is why it exists."""
  code = "import os; print(os.environ.get('AM_POWER_LIMIT', 'unset'))"
  env = {**os.environ, "PYTHONPATH": str(BASEDIR)}
  env.pop("AM_POWER_LIMIT", None)
  result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(BASEDIR), env=env, check=True)
  assert result.stdout.strip() == egpu_patches.AM_POWER_LIMIT


def test_patches_are_not_added_back_to_upstream_files():
  """These patches must live in egpu_patches.py only (see the module docstring)."""
  base = Path(BASEDIR)  # basedir.BASEDIR is a str
  modeld = (base / "openpilot/selfdrive/modeld/modeld.py").read_text()
  compile_modeld = (base / "openpilot/selfdrive/modeld/compile_modeld.py").read_text()
  precompiled_model = (base / "openpilot/selfdrive/modeld/precompiled_model.py").read_text()
  usbgpu = (base / "openpilot/system/hardware/usbgpu.py").read_text()
  launch_env = (base / "launch_env.sh").read_text()
  helpers = (base / "openpilot/selfdrive/modeld/helpers.py").read_text()

  assert "AM_POWER_LIMIT" not in modeld
  assert "AM_POWER_LIMIT" not in launch_env
  assert "_FW_BASES" not in compile_modeld
  assert "chestnut" not in compile_modeld
  assert "USB_TRANSIENT_TEXT" not in precompiled_model
  assert "import openpilot.selfdrive.modeld.compile_modeld" not in usbgpu
  # The delivery pin is applied by patch_model_delivery; helpers.py stays upstream's, so a
  # merge cannot conflict on that logic (chunked_model owns the pin, markers steer the rest).
  assert "read_model_source" not in helpers
