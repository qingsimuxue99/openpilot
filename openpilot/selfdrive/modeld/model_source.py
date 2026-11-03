"""Where the big model and its precompiled artifacts are served from.

Kept in its own file so switching mirrors does not require touching big_model.py:
only this one small module changes.

precompiled.json and firmware/amdgpu are resolved relative to the model URL (see
precompiled_model.ensure_precompiled / firmware.ensure_firmware), so keep the
model file inside <base>/models/.

This module is fork-only: big_model.py only calls pinned_manifest() at the top of
fetch_manifest(). Keep it that way, so a merge cannot conflict on the pin.

Known intentional divergences in upstream files (all "plain http mirror" relaxations,
deletable once the mirror serves TLS, and all greppable by `not in ("http", "https")`):
  * big_model.BigModelManifest.from_dict  - accepts http as well as https
  * big_model.wait_for_manifest_network   - accepts http, and defaults the port by scheme
  * precompiled_model.validate_catalog    - accepts http artifacts on the catalog origin
"""
from __future__ import annotations

MODEL_BASE = "http://op.gitop.vip:82/egpu"

# The branch's own pin, and the extra checkpoints the model picker may offer.
# Retrained Mountain Dew v3 is the pin: the same checkpoint upstream pins (#39087),
# but served from this fork's LAN mirror so a fresh device does not have to pull it
# from the slow NAS. Cinque v3 and the v2 ONNX stay selectable, so a device can
# still fall back to either of them from the model page.
PINNED_MODEL = {
  "model_id": "comma-pr39087-md-v3-e86b58dc-d4c7a3fc",
  "filename": "big_driving_tinygrad.pkl",
  "size": 799_941_965,
  "sha256": "d4c7a3fcfc95813826ca871a80bea18df833aacb2eb6f5333e25a01e65b1a422",
  # precompiled.json and firmware/amdgpu resolve relative to this URL, so each
  # model keeps its own directory under egpu/models/.
  "url": f"{MODEL_BASE}/models/md-v3/big_driving_tinygrad.pkl",
  # Picker metadata (ignored by BigModelManifest.from_dict): the short name this
  # model is listed under, and that it belongs at the top of the list.
  "label": "MD V3",
  "picker_first": True,
}

OPTIONAL_MODELS = (
  {
    "model_id": "comma-pr38932-cinque-v3-892fc3a1-e758b96d",
    "filename": "big_driving_tinygrad.pkl",
    "size": 776_634_338,
    "sha256": "e758b96df27858ea97122d18554930d04f9f8bda417417074edfb3a72b008d0b",
    # Self-hosted like the pin: precompiled.json resolves relative to this URL.
    "url": f"{MODEL_BASE}/models/cinque-v3/big_driving_tinygrad.pkl",
    "label": "CTM V3",
  },
  {
    "model_id": "comma-pr38823-cinque-v2-37bfa141-09d080f3",
    "filename": "big_driving_supercombo.onnx",
    "size": 766_040_736,
    "sha256": "09d080f36965bb2a0790500452bd328aa03c484d0222aa79d1ad9f021a522aec",
    # Self-hosted like the pin: precompiled.json resolves relative to this URL.
    "url": f"{MODEL_BASE}/models/cinque-v2/big_driving_supercombo.onnx",
    "label": "CTM V2",
  },
)

# Schemes the model server may use. Upstream insists on https; this fork's mirror is
# plain http on a LAN, and integrity comes from the mandatory sha256 checks instead.
MODEL_SCHEMES = ("http", "https")


def manifest_from_dict(meta: dict):
  """Build a BigModelManifest from one of the dicts above (lazy: no import cycle)."""
  from openpilot.selfdrive.modeld.big_model import BigModelManifest
  return BigModelManifest.from_dict(dict(meta), meta.get("url", ""))


def optional_manifests() -> list:
  """Validated manifests for OPTIONAL_MODELS; a bad entry is skipped, not raised."""
  out = []
  for meta in OPTIONAL_MODELS:
    try:
      out.append(manifest_from_dict(meta))
    except Exception:
      continue
  return out


def _picker_meta() -> tuple[dict[str, str], tuple[str, ...]]:
  """(sha -> short name, leading shas) collected from the pin and the optional models."""
  labels: dict[str, str] = {}
  leading: list[str] = []
  for meta in (PINNED_MODEL, *OPTIONAL_MODELS):
    sha = meta.get("sha256")
    if not isinstance(sha, str):
      continue
    if isinstance(label := meta.get("label"), str) and label:
      labels.setdefault(sha, label)
    if meta.get("picker_first"):
      leading.append(sha)
  return labels, tuple(dict.fromkeys(leading))


def label_for(sha256: str | None, model_id: str | None = None) -> str | None:
  """Short name for the model picker, or None to keep the caller's fallback.

  The on-device index has no label field and the server catalog may be unreachable
  (LAN mirror down, factory-fresh device), so the branch's own models are named here;
  a label from the server catalog still wins (see egpu_model_select.build_models_payload).
  """
  labels, _ = _picker_meta()
  return labels.get(sha256) if isinstance(sha256, str) else None


def picker_rank(sha256: str | None) -> int:
  """Sort key for the model picker: 0 for the models that are listed first."""
  _, leading = _picker_meta()
  return 0 if isinstance(sha256, str) and sha256 in leading else 1


def pinned_manifest(manifest_url: str):
  """The manifest fetch_manifest() should use, or None to keep upstream's logic.

  A pick in the web UI wins over the pin. An explicit manifest_url (CLI/env
  override) is left to upstream, so this only answers for the branch's default.
  """
  from openpilot.selfdrive.modeld.big_model import DEFAULT_MANIFEST_URL
  from openpilot.selfdrive.modeld.model_catalog import selected_manifest

  chosen = selected_manifest()
  if chosen is not None:
    return chosen
  if manifest_url != DEFAULT_MANIFEST_URL:
    return None
  try:
    return manifest_from_dict(PINNED_MODEL)
  except Exception:
    return None
