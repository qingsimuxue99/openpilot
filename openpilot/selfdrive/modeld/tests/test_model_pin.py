"""Fork behaviour upstream's test_big_model.py cannot assert: this branch's own pin.

Everything here exercises model_source/model_catalog, so tests/test_big_model.py can
stay byte-identical to upstream (see tests/conftest.py for the two xfailed cases).
"""
import pytest
from pathlib import Path

import openpilot.selfdrive.modeld.big_model as big_model
from openpilot.selfdrive.modeld import model_catalog, model_source
from openpilot.selfdrive.modeld.big_model import BigModelManifest

PIN_MODEL_ID = "comma-pr38823-cinque-v2-37bfa141-09d080f3"
V3_MODEL_ID = "comma-pr38932-cinque-v3-892fc3a1-e758b96d"


@pytest.fixture(autouse=True)
def empty_model_index(tmp_path, monkeypatch):
  """Hermetic: point the catalog at an empty directory, so neither the machine's
  model cache nor a previous web pick can influence the assertions below."""
  monkeypatch.setattr(model_catalog, "_cache_dir", lambda cache_dir=None: Path(cache_dir) if cache_dir else tmp_path)


def v3_manifest():
  return BigModelManifest.from_dict(dict(model_source.OPTIONAL_MODELS[0]), model_source.OPTIONAL_MODELS[0]["url"])


def test_pin_is_the_fork_cinque_v2():
  manifest = model_source.pinned_manifest(big_model.DEFAULT_MANIFEST_URL)
  assert manifest is not None
  assert manifest.model_id == PIN_MODEL_ID
  assert manifest.size == 766_040_736
  assert manifest.sha256 == "09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec"
  assert manifest.url == f"{model_source.MODEL_BASE}/models/cinque-v2/big_driving_supercombo.onnx"
  assert not manifest.precompiled_only


def test_fetch_manifest_serves_the_pin_without_the_network(monkeypatch):
  monkeypatch.setattr(big_model, "urlopen", lambda *_args, **_kwargs: pytest.fail("the pin needs no network"))
  manifest = big_model.fetch_manifest()
  assert manifest.model_id == PIN_MODEL_ID
  assert manifest.url.startswith(model_source.MODEL_BASE)


def test_explicit_manifest_url_bypasses_the_pin():
  # An explicit URL (CLI/env override) must keep upstream's remote-fetch path.
  assert model_source.pinned_manifest("https://example.com/models/manifest.json") is None


def test_web_selection_wins_over_the_pin(monkeypatch):
  chosen = v3_manifest()
  monkeypatch.setattr(model_catalog, "selected_manifest", lambda *args, **kwargs: chosen)
  assert model_source.pinned_manifest(big_model.DEFAULT_MANIFEST_URL) is chosen


def test_optional_cinque_v3_is_offered_and_selectable(tmp_path):
  manifests = model_source.optional_manifests()
  assert [m.filename for m in manifests] == ["big_driving_tinygrad.pkl"]
  v3 = manifests[0]
  assert v3.model_id == V3_MODEL_ID
  assert v3.sha256 == "e758b96df27858ea97122d18554930d04f9f8bda417417074edfb3a72b008d0b"
  assert v3.url == f"{model_source.MODEL_BASE}/models/cinque-v3/big_driving_tinygrad.pkl"
  assert v3.precompiled_only
  assert v3.cache_filename == "big_driving_tinygrad-e758b96df27858ea.pkl"

  # The picker lists it even though the index has never seen it, and picking it
  # registers the entry so the downloader has a URL and filename to target.
  listed = {item["sha256"]: item for item in model_catalog.list_models(tmp_path)}
  assert listed[v3.sha256]["builtin"] is True
  assert listed[v3.sha256]["downloaded"] is False
  assert model_catalog.select(v3.sha256, tmp_path)
  assert model_catalog.selected_sha(tmp_path) == v3.sha256
  picked = model_catalog.selected_manifest(tmp_path)
  assert picked is not None and picked.url == v3.url


def test_v3_precompiled_only_never_uses_a_local_onnx_build(tmp_path, monkeypatch):
  # Upstream's equivalent test, with the fork's optional v3 manifest injected by hand
  # (upstream relies on its own default being v3; ours is v2).
  from openpilot.selfdrive.modeld import helpers, precompiled_model

  manifest = v3_manifest()
  monkeypatch.setattr(big_model, 'selected_manifest', lambda: manifest)
  monkeypatch.setattr(helpers, 'selected_manifest', lambda: manifest)
  monkeypatch.setattr(precompiled_model, 'installed', lambda *args: None)
  monkeypatch.setattr(helpers, 'modeld_pkl_path', lambda **kw: pytest.fail('no ONNX compiler artifact for v3'))
  assert not big_model.active_model_compiled()
  assert helpers.active_usbgpu_compiled_path() is None
  assert helpers.usbgpu_compiled_path() is None
