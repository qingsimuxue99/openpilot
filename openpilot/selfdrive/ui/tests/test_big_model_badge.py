"""The REBOOT prompt on the HUD badge.

The requirement is narrow, so the tests are: the prompt may show only for a delivery that
actually landed and is not running yet. Loading, a delivery still moving, and a model that
already runs all have to stay silent - the prompt told drivers to reboot during a normal
startup, which is what these guard.

The module is loaded by path (as test_text_draw_offsets.py does) because importing the UI
package would drag pyray and the whole ui_state into the test process.
"""
import importlib.util
import sys
import time
import types
from pathlib import Path

import pytest

BADGE_PATH = Path(__file__).parents[1] / "big_model_badge.py"


@pytest.fixture
def badge(monkeypatch, tmp_path):
  raylib = types.ModuleType("pyray")
  raylib.Color = lambda *args: tuple(args)
  monkeypatch.setitem(sys.modules, "pyray", raylib)

  monkeypatch.setitem(sys.modules, "openpilot.selfdrive.ui.ui_state", types.SimpleNamespace(
    ui_state=types.SimpleNamespace(usbgpu_present=False, usbgpu_active=False,
                                   usbgpu_loading=False, usbgpu_startup_failed=False)))
  monkeypatch.setitem(sys.modules, "openpilot.system.ui.lib.text_measure", types.SimpleNamespace(
    measure_text_cached=lambda font, text, size: types.SimpleNamespace(x=len(text) * size, y=size)))

  flag = tmp_path / "carrot-big-model-needs-reboot"
  monkeypatch.setitem(sys.modules, "openpilot.selfdrive.modeld.precompiled_fetch",
                      types.SimpleNamespace(REBOOT_FLAG=flag))
  monkeypatch.setitem(sys.modules, "openpilot.selfdrive.modeld.big_model",
                      types.SimpleNamespace(model_cache_dir=lambda: tmp_path))
  monkeypatch.setitem(sys.modules, "openpilot.selfdrive.modeld.big_model_status",
                      types.SimpleNamespace(read_big_model_status=lambda _dir: None))

  values: dict[str, bool] = {}

  class FakeParams:
    def get_bool(self, key):
      return bool(values.get(key, False))

  monkeypatch.setitem(sys.modules, "openpilot.common.params",
                      types.SimpleNamespace(Params=FakeParams))

  module_name = f"_test_big_model_badge_{id(values)}"
  spec = importlib.util.spec_from_file_location(module_name, BADGE_PATH)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  monkeypatch.setitem(sys.modules, module_name, module)
  spec.loader.exec_module(module)

  return types.SimpleNamespace(module=module, flag=flag, values=values)


def style(badge, state, **detail):
  return badge.module.badge_style({"state": state, **detail}, 1.0)


def test_nothing_is_drawn_when_no_delivery_landed(badge):
  assert style(badge, "compiled") is None


def test_the_prompt_shows_after_a_delivery_landed(badge):
  badge.flag.touch()

  lines, _color = style(badge, "compiled")

  assert lines == ["REBOOT"]


def test_loading_the_model_is_not_a_reason_to_reboot(badge):
  badge.flag.touch()
  badge.values["UsbGpuLoading"] = True

  assert style(badge, "compiled") is None


def test_a_delivery_that_is_still_moving_is_not_a_finished_download(badge):
  badge.flag.touch()

  assert style(badge, "verifying", updated_at=time.time()) is None


def test_a_stale_status_still_allows_the_prompt(badge):
  """A reboot mid-verify leaves status.json on "verifying" with nothing running."""
  badge.flag.touch()

  lines, _color = style(badge, "verifying", updated_at=time.time() - 600)

  assert lines == ["REBOOT"]


def test_the_prompt_clears_once_the_model_is_running(badge):
  badge.flag.touch()
  badge.values["UsbGpuActive"] = True

  assert style(badge, "compiled") is None
  # Not just hidden: the flag is gone, so an ignition-on that wipes the params cannot
  # bring the prompt back for the rest of this boot either.
  assert not badge.flag.exists()


def test_download_progress_still_draws_its_two_lines(badge):
  lines, _color = badge.module.badge_style(
    {"state": "downloading", "detail": "pkl", "downloaded_bytes": 450, "total_bytes": 1000}, 0.45)

  assert lines == ["PKL", "45%"]
