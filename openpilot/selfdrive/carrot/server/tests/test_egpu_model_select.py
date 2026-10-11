"""Model picker payload and removal, including the precompiled-only PKL (Cinque v3).

The feature file is loaded from disk with a fully qualified name so its relative import of
services.params resolves, the same way test_egpu_model.py loads features/egpu_model.py.
"""
import asyncio
import importlib.util
from pathlib import Path

import pytest

PKL_SHA = "e758b96df27858ea97122d18554930d04f9f8bda417417074edfb3a72b008d0b"
ONNX_SHA = "09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec"


@pytest.fixture
def select(monkeypatch, tmp_path):
  spec = importlib.util.spec_from_file_location(
    "openpilot.selfdrive.carrot.server.features.egpu_model_select_test",
    Path(__file__).parents[1] / "features/egpu_model_select.py",
  )
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setattr(module, "model_cache_dir", lambda: tmp_path)
  monkeypatch.setattr(module, "_remote_catalog", lambda: None)
  monkeypatch.setattr(module, "_active_sha", lambda: None)
  monkeypatch.setattr(module, "_running_sha", lambda: None)
  monkeypatch.setattr(module, "HAS_PARAMS", False)
  monkeypatch.setattr(module.model_catalog, "selected_sha", lambda *a, **k: None)
  monkeypatch.setattr(module.model_catalog, "read_index", lambda *a, **k: {"selected": None, "models": {}})
  monkeypatch.setattr(module.model_catalog, "write_index", lambda *a, **k: None)
  monkeypatch.setattr(module.model_catalog, "list_models", lambda *a, **k: [])
  return module


def entry(sha: str, filename: str) -> dict:
  return {"model_id": filename, "sha256": sha, "size": 10, "url": f"http://mirror/{filename}",
          "filename": filename, "downloaded": False, "selected": False}


@pytest.fixture
def models(select, monkeypatch):
  entries = [entry(PKL_SHA, "big_driving_tinygrad.pkl"),
             entry(ONNX_SHA, "big_driving_supercombo.onnx")]
  monkeypatch.setattr(select.model_catalog, "list_models", lambda *a, **k: entries)
  return entries


class FakeRequest:
  def __init__(self, body):
    self._body = body

  async def json(self):
    return self._body


def test_payload_flags_only_the_pkl_model_as_precompiled_only(select, models):
  payload = select.build_models_payload()
  flagged = {m["sha256"]: m["precompiled_only"] for m in payload["models"]}

  assert flagged[PKL_SHA] is True
  assert flagged[ONNX_SHA] is False


def test_remove_deletes_the_precompiled_pkl(select, models, tmp_path):
  # The cache name follows the manifest, so the PKL has to be a deletion candidate too:
  # the ONNX name used to be hardcoded here and left Cinque v3's 776 MB on disk (and the
  # page kept reporting the model as downloaded).
  cached = tmp_path / f"big_driving_tinygrad-{PKL_SHA[:16]}.pkl"
  cached.write_bytes(b"model")
  precompiled = tmp_path / "precompiled" / PKL_SHA
  precompiled.mkdir(parents=True)
  (precompiled / "installed.json").write_text("{}", encoding="utf-8")

  response = asyncio.run(select.api_remove(FakeRequest({"sha256": PKL_SHA})))

  assert response.status == 200
  assert cached.exists() is False
  assert precompiled.exists() is False


def test_remove_keeps_working_for_the_pinned_onnx_name(select, monkeypatch, tmp_path):
  monkeypatch.setattr(select.model_catalog, "list_models", lambda *a, **k: [])
  cached = tmp_path / f"big_driving_supercombo-{ONNX_SHA[:16]}.onnx"
  cached.write_bytes(b"model")

  response = asyncio.run(select.api_remove(FakeRequest({"sha256": ONNX_SHA})))

  assert response.status == 200
  assert cached.exists() is False
