# The model "REBOOT" badge (2026-10-16)

Requested behavior: the HUD badge drawn under the eGPU badge shows `REBOOT` **only** after a
model delivery has actually finished downloading. Loading, a delivery still moving, and any
other situation must draw nothing.

## Where the prompt comes from

`openpilot/selfdrive/ui/big_model_badge.py` draws it, and only when all of these hold:

| # | Condition | Source |
|---|---|---|
| 1 | `/tmp/carrot-big-model-needs-reboot` exists | touched by `precompiled_fetch.py` / `chunked_model.py` |
| 2 | `status.json` state is `compiled` / `ready` / `verifying` | `_PROMPT_STATES` |
| 3 | `UsbGpuPresent` | written by `modeld` |
| 4 | `UsbGpuLoading` false **and** `UsbGpuActive` false | written by `modeld` |

The flag lives on tmpfs, so a reboot or power cycle removes it - that part worked. Two holes
made the prompt appear at times nobody had delivered anything:

* **The flag was re-created on every boot.** The precompiled path in `precompiled_fetch.py`
  touched it unconditionally, while the chunked path already guarded on `newly_installed`
  ("a reboot is only needed when something actually landed in this run"). A healthy boot
  therefore always had condition 1 satisfied, while a chunked-only device usually did not -
  the difference between the two devices that reported this.
* **Conditions 3 and 4 are transient.** `manager.py` clears `UsbGpuLoading` /
  `UsbGpuActive` on every ignition-on (`CLEAR_ON_IGNITION_ON`, `params_keys.h:142-143`), and
  `modeld` writes them once, at its own startup, then never refreshes them (its periodic
  refresh at `modeld.py:404-407` covers `UsbGpuPresent` / `UsbGpuCompiled` only). Between the
  wipe and the rewrite the badge read "not loading, not running" and drew `REBOOT` while the
  model was about to run normally. A reboot hid it by also clearing `UsbGpuPresent`
  (`CLEAR_ON_MANAGER_START`), which closes the drawing gate - hence "after a reboot it stops
  showing".

Measured on 1.10 (2026-10-16): zeroing the two params, exactly what an ignition-on does,
made `badge_style()` return `(["REBOOT"], color)` with the gate open; restoring `active=1`
returned `None`. A reboot trace showed the only frame that computed `REBOOT` was blocked by
the gate because `UsbGpuPresent` had not been written yet.

## Fix (fork-only files; no upstream file and no rebuild touched)

* `precompiled_fetch.py`: snapshot `_precompiled_installed(manifest)` before the fetch and
  touch the flag only when the artifact actually landed in this run.
* `big_model_badge.py`:
  * `_delivery_in_flight()` - a delivery that is still moving (`_IN_FLIGHT_STATES`) **and**
    demonstrably alive (`_downloader_running()` or a status file updated within 12 s) is not a
    finished download, so nothing is drawn.
  * `_needs_reboot()` clears the flag as soon as `UsbGpuActive` is true: the artifact is
    running, nothing is pending, and the prompt cannot come back when the next ignition-on
    wipes the params. It can never hide a needed reboot, because the prompt is only drawn
    while `active` is false.

Net effect: `REBOOT` appears only for a delivery that landed and is not running yet, and it
disappears on its own once that artifact runs - not only after the next power cycle.

## Validation

`openpilot/selfdrive/ui/tests/test_big_model_badge.py` (7 tests: no flag, landed delivery,
loading, delivery in flight, stale status, cleared once running, download progress) and
`openpilot/selfdrive/modeld/tests/test_precompiled_fetch.py` (`+2`: a landing delivery asks
for the reboot, an already-installed artifact does not). Both load the module under test with
stubbed pyray / Params / REBOOT_FLAG, so they run without a GPU. Device-level check on 1.10: a healthy boot leaves no flag at all, and clearing UsbGpuLoading / UsbGpuActive (what an ignition-on does) draws nothing, where before the same state produced REBOOT.
