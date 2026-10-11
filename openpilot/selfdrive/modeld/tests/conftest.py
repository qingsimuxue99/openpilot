"""Fork-local test policy for the big model.

The pin, the optional checkpoints and the http-mirror policy all live in
modeld/model_source.py, so tests/test_big_model.py can stay byte-identical to
upstream and a merge has nothing to resolve in it. The two upstream tests that
assert upstream's own pin, plus the parametrised case that asserts https-only,
are marked xfail here; behavioural coverage of the fork's pin lives in
test_model_pin.py.
"""
from pathlib import Path

import pytest

UPSTREAM_FILE = "test_big_model.py"
XFAIL_BY_NAME = {
  # asserts fetch_manifest() pins upstream's Cinque v3
  "test_default_manifest_is_pinned_to_cinque_v3",
  # asserts the default manifest is precompiled-only (upstream's v3 is, our pin is not)
  "test_precompiled_only_model_never_uses_a_local_onnx_build",
}
HTTP_URL_PARAM = "test_manifest_rejects_unsafe_values[url-http://"
PIN_REASON = "this fork pins its own big model; see modeld/model_source.py"
HTTP_REASON = "this fork accepts plain-http mirrors; see modeld/model_source.py"


def pytest_collection_modifyitems(config, items):
  for item in items:
    # Match on the file too: test_model_pin.py deliberately reuses one of these names.
    if Path(str(item.fspath)).name != UPSTREAM_FILE:
      continue
    if item.name in XFAIL_BY_NAME:
      item.add_marker(pytest.mark.xfail(reason=PIN_REASON, strict=False))
    elif item.name.startswith(HTTP_URL_PARAM):
      item.add_marker(pytest.mark.xfail(reason=HTTP_REASON, strict=False))
