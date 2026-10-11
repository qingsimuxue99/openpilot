"""Delivery rules for the precompiled-only (generic PKL) checkpoint.

tests/conftest.py keeps test_big_model.py byte-identical to upstream, so the coverage of
this fork's precompiled-only handling lives here: such a model *is* the delivery file, so
it has no ONNX to compile locally and no chunked set to install, and a pinned "chunked"
delivery must not be able to leave a selected model unusable.
"""
from pathlib import Path

import pytest

from openpilot.selfdrive.modeld import chunked_model, firmware, precompiled_fetch, precompiled_model
from openpilot.selfdrive.modeld.big_model import BigModelManifest

V3_SHA = "e758b96df27858ea97122d18554930d04f9f8bda417417074edfb3a72b008d0b"
V3_URL = "http://mirror/egpu/models/cinque-v3/big_driving_tinygrad.pkl"
LEGACY_SHA = "09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec"


def manifest(filename: str = "big_driving_tinygrad.pkl", sha: str = V3_SHA) -> BigModelManifest:
  url = V3_URL if filename.endswith(".pkl") else V3_URL.replace("big_driving_tinygrad.pkl", filename)
  # from_dict() reads the model URL from the value itself (its second argument is only the
  # manifest base used to resolve it), so the url belongs in the dict.
  return BigModelManifest.from_dict({"model_id": "test-model", "filename": filename, "url": url,
                                     "size": 10, "sha256": sha})


class Reporter:
  def __init__(self):
    self.states: list[str] = []
    self.values: list[dict] = []

  def update(self, state: str, **values) -> None:
    self.states.append(state)
    self.values.append(values)


@pytest.fixture
def calls(monkeypatch, tmp_path):
  """Stub the download/disk sides so only the delivery decision is exercised."""
  recorded = {"chunked": 0, "precompiled": 0, "cleared": 0, "synced": 0, "host_waits": 0}

  def count(name):
    def call(*_args, **_kwargs):
      recorded[name] += 1
      return None
    return call

  def ensure_chunked(*_args, **_kwargs):
    recorded["chunked"] += 1
    raise RuntimeError("no chunked section in this catalog")

  def ensure_precompiled(*_args, **_kwargs):
    recorded["precompiled"] += 1
    return tmp_path / "model.pkl"

  def wait_for_host(*_args, **_kwargs):
    # The real one blocks on the model host; keep the tests offline and instant.
    recorded["host_waits"] += 1
    return True

  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: "auto")
  monkeypatch.setattr(chunked_model, "installed_chunked", lambda *_a, **_k: None)
  monkeypatch.setattr(chunked_model, "ensure_chunked", ensure_chunked)
  monkeypatch.setattr(chunked_model, "clear_precompiled_marker", count("cleared"))
  monkeypatch.setattr(chunked_model, "sync_precompiled_marker", count("synced"))
  monkeypatch.setattr(firmware, "ensure_firmware", lambda *_a, **_k: None)
  monkeypatch.setattr(precompiled_model, "ensure_precompiled", ensure_precompiled)
  monkeypatch.setattr(precompiled_fetch, "patch_runtime_firmware_paths", lambda *_a, **_k: None)
  monkeypatch.setattr(precompiled_fetch, "REBOOT_FLAG", tmp_path / "reboot")
  monkeypatch.setattr(precompiled_fetch, "_precompiled_rejected", lambda _m: False)
  monkeypatch.setattr(precompiled_fetch, "_wait_for_host", wait_for_host)
  return recorded


def test_precompiled_only_model_is_installed_even_with_a_pinned_chunked_delivery(calls, monkeypatch):
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: "chunked")
  reporter = Reporter()

  precompiled_fetch.fetch_after_onnx(manifest(), reporter)

  # The chunked set cannot exist for this model, so it must not even be attempted, and the
  # precompiled artifact (which is the model) has to be fetched instead of skipped.
  assert calls["chunked"] == 0
  assert calls["precompiled"] == 1
  assert calls["cleared"] == 1


def test_pinned_chunked_still_wins_for_a_model_with_an_onnx(calls, monkeypatch):
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: "chunked")
  reporter = Reporter()

  # The pinned delivery did not land (the stub catalog has no chunked section): that has to
  # surface as an error, otherwise the launcher never retries and the pages keep saying ready.
  with pytest.raises(RuntimeError, match="chunked model fetch failed"):
    precompiled_fetch.fetch_after_onnx(manifest("big_driving_supercombo.onnx", LEGACY_SHA), reporter)

  assert calls["chunked"] == 1
  assert calls["precompiled"] == 0


def test_auto_delivery_installs_precompiled_only_model(calls):
  reporter = Reporter()

  precompiled_fetch.fetch_after_onnx(manifest(), reporter)

  assert calls["chunked"] == 1  # tried first, as upstream's auto ordering does
  assert calls["precompiled"] == 1
  assert calls["cleared"] == 1


def test_rejected_precompiled_only_model_is_reported_not_refetched(calls, monkeypatch):
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: "chunked")
  monkeypatch.setattr(precompiled_fetch, "_precompiled_rejected", lambda _m: True)
  reporter = Reporter()

  with pytest.raises(RuntimeError, match="rejected"):
    precompiled_fetch.fetch_after_onnx(manifest(), reporter)

  assert calls["precompiled"] == 0
  assert calls["cleared"] == 0  # the rejection marker protects the device from a retry loop
  assert reporter.states == ["error"]
  assert "rejected" in reporter.values[0]["detail"]


def test_pinned_precompiled_fetch_failure_is_retried_and_reported(calls, monkeypatch):
  """The user picked this delivery, so a DNS hiccup must not become a silent deferral.

  launch_chffrplus.sh retries `big_model --ensure-if-egpu` only while status.json says
  "error" (big_model_update_failed), and big_model.py writes that error when this raises.
  """
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: "precompiled")
  attempts = {"n": 0}

  def failing_ensure_precompiled(*_args, **_kwargs):
    attempts["n"] += 1
    raise OSError("[Errno -3] Temporary failure in name resolution")

  monkeypatch.setattr(precompiled_model, "ensure_precompiled", failing_ensure_precompiled)
  reporter = Reporter()

  with pytest.raises(RuntimeError, match="precompiled model fetch failed"):
    precompiled_fetch.fetch_after_onnx(manifest("big_driving_supercombo.onnx", LEGACY_SHA), reporter)

  assert attempts["n"] == precompiled_fetch.FETCH_ATTEMPTS  # retried instead of giving up once
  assert calls["host_waits"] >= 1                            # waited for the catalog host first
  assert calls["precompiled"] == 0                           # the counting stub was replaced


def test_automatic_delivery_keeps_a_failed_precompiled_fetch_deferred(calls, monkeypatch):
  """auto + ONNX: the local compiler is still a fallback, so nothing escalates."""
  def failing_ensure_precompiled(*_args, **_kwargs):
    raise OSError("[Errno -3] Temporary failure in name resolution")

  monkeypatch.setattr(precompiled_model, "ensure_precompiled", failing_ensure_precompiled)
  reporter = Reporter()

  precompiled_fetch.fetch_after_onnx(manifest("big_driving_supercombo.onnx", LEGACY_SHA), reporter)
  assert reporter.states == []  # no "error" state: the next boot simply tries again


@pytest.mark.parametrize("source,filename,expected", [
  ("precompiled", "big_driving_supercombo.onnx", True),   # the user pinned this delivery
  ("auto", "big_driving_tinygrad.pkl", True),             # a generic PKL *is* the precompiled artifact
  ("auto", "big_driving_supercombo.onnx", False),         # chunked or the local compiler can serve it
  ("chunked", "big_driving_supercombo.onnx", False),
])
def test_precompiled_required_follows_the_selection(source, filename, expected, monkeypatch):
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: source)
  sha = V3_SHA if filename.endswith(".pkl") else LEGACY_SHA
  assert precompiled_fetch.precompiled_required(manifest(filename, sha)) is expected


def test_rejected_marker_lookup_uses_the_cache_dir(monkeypatch, tmp_path):
  monkeypatch.setattr(precompiled_fetch, "Path", Path, raising=False)
  import openpilot.selfdrive.modeld.big_model as big_model
  monkeypatch.setattr(big_model, "model_cache_dir", lambda: tmp_path)

  assert precompiled_fetch._precompiled_rejected(manifest()) is False
  marker = tmp_path / "precompiled" / V3_SHA / "rejected"
  marker.parent.mkdir(parents=True)
  marker.write_text(V3_SHA)
  assert precompiled_fetch._precompiled_rejected(manifest()) is True


def test_auto_install_leaves_a_precompiled_only_model_to_the_precompiled_path(monkeypatch):
  monkeypatch.setattr(chunked_model, "read_model_source", lambda *_a, **_k: "chunked")
  monkeypatch.setattr(chunked_model, "ensure_firmware", lambda *_a, **_k: None)
  monkeypatch.setattr(chunked_model, "rearm_usbgpu", lambda *_a, **_k: True)
  monkeypatch.setattr(chunked_model, "ensure_chunked",
                      lambda *_a, **_k: pytest.fail("the chunked set cannot exist for a PKL model"))
  monkeypatch.setattr(chunked_model, "clear_precompiled_marker",
                      lambda *_a, **_k: pytest.fail("a rejection marker must survive the boot hook"))

  assert chunked_model.auto_install(manifest()) is None


def test_boot_hook_names_the_precompiled_delivery_for_a_pkl_model(monkeypatch, tmp_path):
  monkeypatch.setattr(chunked_model, "model_cache_dir", lambda: tmp_path)
  monkeypatch.setattr(chunked_model, "auto_install", lambda *_a, **_k: None)
  monkeypatch.setattr(chunked_model, "download_deferred", lambda *_a, **_k: True)

  assert chunked_model.boot_hook(manifest()) is True
  status = (tmp_path / "status.json").read_text(encoding="utf-8")
  assert "precompiled model in background" in status
