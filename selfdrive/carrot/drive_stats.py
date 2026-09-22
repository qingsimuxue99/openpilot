#!/usr/bin/env python3
"""Drive stats accumulator + software RTC fallback.

Runs as a always-on background process. Subscribes to carState and accumulates
per-day driving distance / duration / trip count, then writes a JSON summary
that the offroad home UI renders as a "driving data" panel.

Data is stored as a plain JSON file (NOT a Params key, to avoid the compiled
whitelist). The UI reads the same file directly:
  {"today": {km, min, trips}, "week": {...}, "total": {...},
   "daily": {YYYY-MM-DD: km, ...}, "date": YYYY-MM-DD}

Historical data starts from zero (device has no pre-existing odometer feed).

Software RTC fallback (c3 has NO hardware RTC):
  We persist the last known wall-clock time to CLOCK_TS on /data (which
  survives power loss). On startup, if the system clock is clearly behind that
  record (e.g. just booted and reset to 1970), we restore it via `date -s`.
  While online, systemd-timesyncd keeps the clock accurate and overrides this.
  This keeps the device showing a sane date/time even offline, like a clock.
"""

import json
import os
import time
import subprocess
from datetime import date

import cereal.messaging as messaging
from openpilot.common.realtime import Ratekeeper

# NOTE: keep these OUTSIDE /data/params on purpose.
#   1) system/manager/manager.py calls Params.clearAll(CLEAR_ON_MANAGER_START) on
#      every boot, and common/params.cc deletes every file in the params dir that
#      is not a key in common/params_keys.h -> these JSON/timestamp files were
#      wiped on every reboot when they lived in the params dir.
#   2) /data/params/d* is a symlink to a per-install temp dir, so a hardcoded
#      /data/params/d_tmp no longer resolves to the active params dir on a fresh
#      install (the UI "reset calibration" action pointed there as well).
# DRIVE_STATS_DIR exists so a simulation harness can point this process at a
# scratch directory instead of the real one.
STATS_DIR = os.environ.get("DRIVE_STATS_DIR", "/data/drive_stats")
STATS_FILE = os.path.join(STATS_DIR, "drive_stats.json")
CLOCK_TS = os.path.join(STATS_DIR, "clock.ts")
LEGACY_DIR = "/data/params/d_tmp"  # pre 2026-09-21 location, migrated once
# Timestamps below this are considered bogus (1970 epoch, or the AGNOS default
# bogus date ~2025-06-04). They must never be persisted/restored, or they would
# poison the software-RTC fallback. 1700000000 == 2023-11-14.
SANE_EPOCH = 1700000000.0
SAVE_INTERVAL = 30.0  # seconds
RATE = 20             # Hz

# ---- odometer accumulation parameters ----
# NOTE: this process must key off fields that ACTUALLY exist on cereal's
# CarState. CarState has no `controlsAllowed` / `enabled` member (those live on
# other structs), so reading them raised AttributeError and killed the process
# on the very first carState frame -> the panel showed all zeros forever even
# though the car was driven. Stick to vEgo (@1) / standstill (@18).
MOVE_V = 0.3      # m/s; below this the car counts as stationary
MAX_DT = 0.5      # s; accumulate() clamps every frame's dt to [0, MAX_DT]. Without
                  # it a paused process (or a carState publisher gap) would credit
                  # one huge dt to a single frame and inflate the odometer.
TRIP_GAP = 180.0  # s; a stop longer than this makes the next move a new "trip"


def _empty_bucket():
  return {"km": 0.0, "min": 0.0, "trips": 0}


def _empty_stats():
  return {"today": _empty_bucket(), "week": _empty_bucket(),
          "total": _empty_bucket(), "daily": {}, "date": ""}


def _iso_week(d):
  """Return (iso_year, iso_week) for an ISO-format date string."""
  return date.fromisoformat(d).isocalendar()[:2]


def accumulate(stats, v, dt, idle_secs):
  """Add one frame of driving to `stats`. Pure function (no IO) so a
  simulation harness can drive it directly.

  v         current speed, m/s (pass abs() of vEgo)
  dt        seconds since the previous frame; clamped to [0, MAX_DT] right here
            so that no caller can credit one huge frame to the odometer
  idle_secs how long the car has been stationary as of the previous frame
  Returns the updated idle_secs.
  """
  dt = min(max(dt, 0.0), MAX_DT)
  if v > MOVE_V and dt > 0.0:
    dist_km = v * dt / 1000.0
    for b in ("today", "week", "total"):
      stats[b]["km"] += dist_km
      stats[b]["min"] += dt / 60.0
    # one trip == resuming after a long stop (waiting at a red light is not a
    # new trip)
    if idle_secs >= TRIP_GAP:
      for b in ("today", "week", "total"):
        stats[b]["trips"] += 1
    return 0.0
  return idle_secs + dt


def roll_over(stats, today, last_date):
  """Midnight / ISO-week roll-over. Pure function.

  Archives today's km into daily[last_date], resets today's bucket, trims
  daily down to the last 7 entries, and resets the week bucket ONLY when the
  day being closed (last_date) and the new day (today) fall in DIFFERENT ISO
  weeks. Comparing the two dates directly (instead of a week number captured
  at process start) is critical: the old code kept a `last_week` from daemon
  startup, so the first rollover after a Monday would wrongly zero the *current*
  week's mileage even though we were still inside that week.

  Returns the updated last_date.
  """
  if not last_date or today == last_date:
    return last_date
  stats["daily"][last_date] = round(stats["today"]["km"], 2)
  # 跨 ISO 周（周一界线）才清零本周；同一周内跨天不清零
  if _iso_week(last_date) != _iso_week(today):
    stats["week"] = _empty_bucket()
  stats["today"] = _empty_bucket()
  for k in sorted(stats["daily"].keys())[:-7]:
    stats["daily"].pop(k, None)
  stats["date"] = today
  return today


def load_stats():
  try:
    if os.path.exists(STATS_FILE):
      with open(STATS_FILE, "r") as f:
        d = json.load(f)
      for k in ("today", "week", "total"):
        if not isinstance(d.get(k), dict):
          d[k] = _empty_bucket()
      if not isinstance(d.get("daily"), dict):
        d["daily"] = {}
      d.setdefault("date", "")
      return d
  except Exception:
    pass
  return _empty_stats()


def save_stats(stats):
  try:
    d = os.path.dirname(STATS_FILE)
    if d:
      os.makedirs(d, exist_ok=True)
    tmp = STATS_FILE + ".tmp"
    with open(tmp, "w") as f:
      json.dump(stats, f)
    os.replace(tmp, STATS_FILE)
  except Exception:
    pass


def save_clock_ts():
  """Persist current wall-clock time so it survives power loss."""
  try:
    d = os.path.dirname(CLOCK_TS)
    if d:
      os.makedirs(d, exist_ok=True)
    with open(CLOCK_TS, "w") as f:
      f.write("%.3f" % time.time())
  except Exception:
    pass


def restore_clock_ts():
  """If the system clock is clearly behind our last persisted (sane) time,
  e.g. just booted with no RTC and reset to a bogus default, restore it via
  `date -s`. This process runs as a non-root user without CAP_SYS_TIME, so we
  use NOPASSWD sudo. Fails silently when offline/unset; systemd-timesyncd
  overrides this once NTP syncs."""
  try:
    if not os.path.exists(CLOCK_TS):
      return
    with open(CLOCK_TS) as f:
      saved = float(f.read().strip())
    if saved < SANE_EPOCH:
      return
    now = time.time()
    # Only restore when the clock is clearly behind the record
    if saved > now + 1.0:
      subprocess.run(["sudo", "-n", "/bin/date", "-s", "@%d" % int(saved)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     check=False)
  except Exception:
    pass


def migrate_legacy():
  """One-time best-effort move of stats/clock files out of the params dir.

  Files sitting in the params dir but not registered in common/params_keys.h get
  unlinked on every boot, so on most devices there is nothing left to migrate;
  this only rescues devices whose params dir was not the active one."""
  try:
    os.makedirs(STATS_DIR, exist_ok=True)
    for name in ("drive_stats.json", "clock.ts"):
      old = os.path.join(LEGACY_DIR, name)
      new = os.path.join(STATS_DIR, name)
      if not os.path.exists(new) and os.path.exists(old):
        with open(old, "rb") as s, open(new, "wb") as d:
          d.write(s.read())
  except Exception:
    pass


def main():
  sm = messaging.SubMaster(['carState'])
  rk = Ratekeeper(RATE, print_delay_threshold=None)

  migrate_legacy()
  stats = load_stats()
  idle_secs = 1e9  # pretend it has been parked, so the first move is a trip
  last_save = 0.0
  last_t = time.monotonic()
  # 用文件里记录的 date 作为初始 last_date，而不是 date.today()：
  # 否则 reboot 若跨过午夜，上一日累积在 today 桶里的里程永远不会被归档进 daily，
  # 也会被错误清零。week 的跨周判定改由 roll_over 直接比对两个日期，不再依赖此处。
  last_date = stats.get("date") or date.today().isoformat()
  stats["date"] = last_date

  # Software RTC: bring the clock back from persisted time if it reset
  restore_clock_ts()
  save_clock_ts()  # persist immediately so a fresh boot always has a record

  while True:
    sm.update()
    now = time.monotonic()
    dt = now - last_t
    last_t = now

    # A single bad frame must NEVER kill this daemon. Regression: the original
    # code read cs.controlsAllowed, which does not exist on cereal CarState, so
    # the process died with AttributeError the instant the car started
    # publishing carState and the odometer stayed at 0 forever.
    try:
      if sm.updated['carState']:
        idle_secs = accumulate(stats, abs(sm['carState'].vEgo), dt, idle_secs)
    except Exception:
      pass

    # midnight / week roll-over
    try:
      last_date = roll_over(stats, date.today().isoformat(), last_date)
    except Exception:
      pass

    if now - last_save > SAVE_INTERVAL:
      save_stats(stats)
      save_clock_ts()
      last_save = now

    rk.keep_time()


if __name__ == "__main__":
  main()
