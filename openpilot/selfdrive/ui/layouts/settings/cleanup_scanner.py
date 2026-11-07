"""设备垃圾扫描器 —— 供「垃圾清理」设置页使用，只依赖 Python 标准库。

最高原则：不能误删。规则与 /data/cleanup_device_garbage.sh 逐条对齐，
本文件是规则唯一来源，界面上显示的「保护规则」由 rule_sections() 自动生成。

五道闸：
  1. 默认只扫描、只出清单，绝不删除任何文件；
  2. 删除只接受「本次扫描清单里的路径」，且必须有用户勾选 + 弹窗确认；
  3. 默认删除 = 移入隔离区 /data/carrot_trash/（可恢复），彻底删除要单独二次确认；
  4. 每个候选逐项复验：保护名单 / 严格文件名 / 普通文件非符号链接 /
     跳过最近 5 分钟仍在写的文件 / 超过 14 天 / 每类保留最近 10 份；
  5. 逐项处理，每 10 项核验一次关键文件仍在，任何异常立即停止。

扫描阶段就排除受保护文件：受保护项根本不会进入结果列表。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time

# ---------------------------------------------------------------------------
# 路径与规则常量
# ---------------------------------------------------------------------------
REPO = "/data/openpilot"
PACK_DIR = REPO + "/.git/objects/pack"
MEDIA = "/data/media/0"
VIDEO_DIR = MEDIA + "/videos"
REALDATA = MEDIA + "/realdata"
LOG_DIR = "/data/log"
BACKUP_DIR = "/data/carrot_backup"
REPORT_DIR = BACKUP_DIR + "/cleanup_reports"
TRASH_ROOT = "/data/carrot_trash"
STATE_DIR = "/data/carrot_state"
COMMAND_SCRIPT = "/data/cleanup_device_garbage.sh"

# 自建状态一律放 /data/carrot_state/；/data/params/d/ 会被 manager 开机清空
SCAN_FILE = STATE_DIR + "/cleanup_scan.json"
SETTINGS_FILE = STATE_DIR + "/cleanup_settings.json"
MANIFEST_FILE = STATE_DIR + "/cleanup_trash_manifest.json"
REPORT_FILE = STATE_DIR + "/cleanup_report.txt"

OLDER_THAN_DAYS = 14
KEEP_RECENT = 10
ACTIVE_MINUTES = 5
BATCH_SIZE = 10

CATEGORY_ORDER = ("git_tmp", "video", "rlog", "swaglog", "cache", "tmp_debug", "root_debug")

CATEGORIES = {
  "git_tmp": {
    "name": "中断的 git 临时包",
    "paths": [PACK_DIR + "/tmp_pack_*"],
    "role": "git fetch（联网拉取）中断时留下的半成品临时包，git 不会再读取。",
    "effect": "不影响仓库对象、当前分支与已提交代码；只释放磁盘空间。",
    "risk": "低",
    "rule": "仅 tmp_pack_ 前缀；排除 .pack/.idx/.rev/.bitmap/.keep/.promisor；跳过最近 5 分钟仍在写的文件。",
  },
  "video": {
    "name": "旧行车录像",
    "paths": [VIDEO_DIR + "/*.mp4"],
    "role": "行车记录仪录下的历史视频，用于事后回看。",
    "effect": "删掉后该段录像看不到了（移入隔离区期间仍可恢复）。",
    "risk": "中 · 涉及个人录像",
    "rule": "文件名严格为 YYYYMMDD-HHMMSS.mp4；超过 14 天；按时间保留最近 10 份。",
  },
  "rlog": {
    "name": "旧行车日志",
    "paths": [REALDATA + "/*/rlog.zst", REALDATA + "/*/qlog.zst"],
    "role": "每趟车产生的 rlog/qlog 压缩日志，排查实车问题时用。",
    "effect": "删掉后该趟车无法再回放日志（移入隔离区期间仍可恢复）。",
    "risk": "中 · 排查资料",
    "rule": "仅标准路段目录名下的 rlog.zst / qlog.zst；目录超过 14 天；保留最近 10 趟。",
  },
  "swaglog": {
    "name": "旧系统日志",
    "paths": [LOG_DIR + "/swaglog.<数字>"],
    "role": "openpilot 各进程写出的历史运行日志。",
    "effect": "只丢旧日志文本，不影响当前运行与正在写的日志。",
    "risk": "低",
    "rule": "文件名严格为 swaglog.纯数字；超过 14 天；保留最近 10 份。",
  },
  "cache": {
    "name": "过期编译缓存",
    "paths": ["/data/scons_cache/*", "/data/uv_cache/*"],
    "role": "scons 编译缓存与 uv 依赖缓存，用来加速下一次编译/安装。",
    "effect": "只删文件不删目录；下次编译会慢一些，不影响功能与已编译产物。",
    "risk": "低",
    "rule": "仅这两个缓存目录内的普通文件；超过 14 天；跳过最近 5 分钟活跃文件。",
  },
  "tmp_debug": {
    "name": "临时调试文件（/tmp）",
    "paths": ["/tmp/_*.py", "/tmp/_*.sh", "/tmp/_*.log", "/tmp/_*.txt"],
    "role": "调试时临时放到 /tmp 的探针脚本、输出与日志。",
    "effect": "只删临时产物；脚本本身不参与开机，不影响系统进程。",
    "risk": "低",
    "rule": "仅 /tmp 根目录、下划线开头的 .py/.sh/.log/.txt；跳过最近 5 分钟活跃文件。",
  },
  "root_debug": {
    "name": "临时调试文件（/data）",
    "paths": ["/data/_*.py", "/data/_*.sh", "/data/_*.log", "/data/_*.txt"],
    "role": "调试时留在 /data 根目录的临时脚本、诊断输出。",
    "effect": "只删临时产物；开机脚本、参数备份、源码草稿都有独立保护，不会命中。",
    "risk": "低",
    "rule": "仅 /data 根目录、下划线开头的 .py/.sh/.log/.txt；跳过最近 5 分钟活跃文件。",
  },
}

# 精确保护：路径完全相同就永不扫描、永不列出、永不删除
PROTECT_EXACT = (
  "/data/continue.sh",
  "/data/wlan_selfheal.sh",
  "/data/backup_params.json",
  "/data/toggle_values.json",
  "/data/carrot_ip.cc",
  "/data/pythonpath",
  "/data/openpilot_backup.tar.gz",
  STATE_DIR,
  COMMAND_SCRIPT,
)

PROTECT_REASONS = {
  "/data/continue.sh": "开机启动脚本，删了设备跑不起来",
  "/data/wlan_selfheal.sh": "WiFi 保守自愈脚本，属于常驻功能",
  "/data/backup_params.json": "c3_toolbox 正在读写的参数备份",
  "/data/toggle_values.json": "c3_toolbox 正在读写的开关值",
  "/data/carrot_ip.cc": "用户源码草稿，不是垃圾",
  "/data/pythonpath": "系统软链，删了跨机 import 会断",
  "/data/openpilot_backup.tar.gz": "整包回滚备份",
  STATE_DIR: "本功能与模型菜单的状态目录（自建状态禁放 /data/params/d/）",
  COMMAND_SCRIPT: "命令行清理脚本自身",
}

# 前缀保护：以这些开头的路径一律排除。
# 唯一例外：git_tmp 类别允许进入 REPO 目录，但仍被下面的严格文件名约束挡住。
PROTECT_PREFIX = (
  REPO + "/",
  MEDIA + "/carrot/",
  BACKUP_DIR + "/",
  TRASH_ROOT + "/",
  "/data/params/",
  "/tmp/carrot_",
  "/tmp/systemd-private-",
  "/tmp/tmux-",
  "/tmp/cg_",
)

PROTECT_PREFIX_REASONS = {
  REPO + "/": "openpilot 源码与 .git 有效对象；唯一例外是 git 中断临时包（另有严格文件名约束）",
  MEDIA + "/carrot/": "大模型与模型状态文件，正在被使用，不是垃圾",
  BACKUP_DIR + "/": "全部回滚快照与清理报告",
  TRASH_ROOT + "/": "隔离区，等用户二次确认后才彻底删除",
  "/data/params/": "参数目录，由 manager 开机管理",
  "/tmp/carrot_": "carrot 常驻进程临时目录",
  "/tmp/systemd-private-": "systemd 私有临时目录",
  "/tmp/tmux-": "tmux 会话目录",
  "/tmp/cg_": "命令行清理脚本的临时工作文件",
}

# 删除前后必须核验仍在的关键文件/目录
VERIFY_KEEP = (
  "/data/continue.sh",
  "/data/wlan_selfheal.sh",
  "/data/backup_params.json",
  "/data/toggle_values.json",
  "/data/carrot_ip.cc",
  BACKUP_DIR,
  REPO + "/.git/HEAD",
  MEDIA + "/carrot/models/models.json",
  "/data/params/d",
)

VIDEO_NAME = re.compile(r"^\d{8}-\d{6}\.mp4$")
RLOG_DIR_NAME = re.compile(r"^[0-9a-fA-F]+--[0-9a-fA-F]+--[0-9]")
SWAGLOG_NAME = re.compile(r"^swaglog\.\d+$")
DEBUG_NAME = re.compile(r"^_.*\.(py|sh|log|txt)$")
PACK_KEEP_SUFFIXES = (".pack", ".idx", ".rev", ".bitmap", ".keep", ".promisor")


# ---------------------------------------------------------------------------
# 保护判定（与命令行脚本 is_protected 等价）
# ---------------------------------------------------------------------------
def is_protected(path: str, category: str) -> bool:
  """返回 True = 受保护，扫描/删除都必须跳过。"""
  if path in PROTECT_EXACT:
    return True
  for prefix in PROTECT_PREFIX:
    if path.startswith(prefix):
      if category == "git_tmp" and prefix == REPO + "/":
        continue
      return True
  if category == "git_tmp":
    if os.path.dirname(path) != PACK_DIR:
      return True
    base = os.path.basename(path)
    if not base.startswith("tmp_pack_"):
      return True
    if base.endswith(PACK_KEEP_SUFFIXES):
      return True
  return False


def verify_keep() -> list[str]:
  return [p for p in VERIFY_KEEP if not os.path.exists(p)]


def selftest() -> dict:
  """自检：证明保护名单拦得住关键文件，且正常垃圾不会被误判为保护。"""
  protected_samples = list(PROTECT_EXACT) + [
    REPO + "/openpilot/common/params_keys.h",
    PACK_DIR + "/pack-abc123.pack",
    MEDIA + "/carrot/models/big_driving_tinygrad-b4dead3ee091d66a.pkl",
    BACKUP_DIR + "/current_state_20261107_154200/README.txt",
    "/data/params/d/SshEnabled",
  ]
  deletable_samples = [
    ("git_tmp", PACK_DIR + "/tmp_pack_TEST123"),
    ("video", VIDEO_DIR + "/20261018-142338.mp4"),
    ("tmp_debug", "/tmp/_debug.py"),
    ("root_debug", "/data/_probe.log"),
  ]
  bad_protected = [p for p in protected_samples if not is_protected(p, "git_tmp")]
  bad_deletable = [p for cat, p in deletable_samples if is_protected(p, cat)]
  return {
    "ok": not bad_protected and not bad_deletable,
    "protected_total": len(protected_samples),
    "protected_fail": bad_protected,
    "deletable_total": len(deletable_samples),
    "deletable_fail": bad_deletable,
    "missing_keep": verify_keep(),
    "keep_total": len(VERIFY_KEEP),
  }


# ---------------------------------------------------------------------------
# 候选收集：只收「确定形态」的文件
# ---------------------------------------------------------------------------
def _is_recent(mtime: float) -> bool:
  return (time.time() - mtime) < ACTIVE_MINUTES * 60


def _is_old(mtime: float) -> bool:
  return (time.time() - mtime) >= OLDER_THAN_DAYS * 86400


def _scandir(path: str):
  try:
    return list(os.scandir(path))
  except OSError:
    return []


def collect_git_tmp() -> list[str]:
  out = []
  for e in _scandir(PACK_DIR):
    if not e.name.startswith("tmp_pack_") or e.name.endswith(PACK_KEEP_SUFFIXES):
      continue
    try:
      if not e.is_file(follow_symlinks=False):
        continue
    except OSError:
      continue
    out.append(e.path)
  return out


def collect_video() -> list[str]:
  pairs = []
  for e in _scandir(VIDEO_DIR):
    if not VIDEO_NAME.match(e.name):
      continue
    try:
      if not e.is_file(follow_symlinks=False):
        continue
      st = e.stat(follow_symlinks=False)
    except OSError:
      continue
    if not _is_old(st.st_mtime) or _is_recent(st.st_mtime):
      continue
    pairs.append((st.st_mtime, e.path))
  pairs.sort(reverse=True)
  return [p for _, p in pairs[KEEP_RECENT:]]


def collect_rlog() -> list[str]:
  dirs = []
  for e in _scandir(REALDATA):
    if not RLOG_DIR_NAME.match(e.name):
      continue
    try:
      if not e.is_dir(follow_symlinks=False):
        continue
      st = e.stat(follow_symlinks=False)
    except OSError:
      continue
    if not _is_old(st.st_mtime):
      continue
    dirs.append((st.st_mtime, e.path))
  dirs.sort(reverse=True)
  out = []
  for _, d in dirs[KEEP_RECENT:]:
    for name in ("rlog.zst", "qlog.zst"):
      p = os.path.join(d, name)
      if os.path.isfile(p) and not os.path.islink(p):
        out.append(p)
  return out


def collect_swaglog() -> list[str]:
  pairs = []
  for e in _scandir(LOG_DIR):
    if not SWAGLOG_NAME.match(e.name):
      continue
    try:
      if not e.is_file(follow_symlinks=False):
        continue
      st = e.stat(follow_symlinks=False)
    except OSError:
      continue
    if not _is_old(st.st_mtime) or _is_recent(st.st_mtime):
      continue
    pairs.append((st.st_mtime, e.path))
  pairs.sort(reverse=True)
  return [p for _, p in pairs[KEEP_RECENT:]]


def collect_cache() -> list[str]:
  out = []
  for root in ("/data/scons_cache", "/data/uv_cache"):
    if not os.path.isdir(root):
      continue
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
      dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
      for name in filenames:
        p = os.path.join(dirpath, name)
        try:
          st = os.lstat(p)
        except OSError:
          continue
        if not stat.S_ISREG(st.st_mode) or not _is_old(st.st_mtime) or _is_recent(st.st_mtime):
          continue
        out.append(p)
  return out


def collect_debug(root: str) -> list[str]:
  out = []
  for e in _scandir(root):
    if not DEBUG_NAME.match(e.name):
      continue
    try:
      if not e.is_file(follow_symlinks=False):
        continue
      st = e.stat(follow_symlinks=False)
    except OSError:
      continue
    if _is_recent(st.st_mtime):
      continue
    out.append(e.path)
  return out


COLLECTORS = (
  ("git_tmp", collect_git_tmp),
  ("video", collect_video),
  ("rlog", collect_rlog),
  ("swaglog", collect_swaglog),
  ("cache", collect_cache),
  ("tmp_debug", lambda: collect_debug("/tmp")),
  ("root_debug", lambda: collect_debug("/data")),
)


def _make_item(path: str, category: str) -> dict | None:
  try:
    st = os.lstat(path)
  except OSError:
    return None
  if not stat.S_ISREG(st.st_mode) or _is_recent(st.st_mtime):
    return None
  c = CATEGORIES[category]
  return {
    "path": path,
    "size": st.st_size,
    "mtime": st.st_mtime,
    "category": category,
    "name": c["name"],
    "role": c["role"],
    "effect": c["effect"],
    "risk": c["risk"],
    "rule": c["rule"],
  }


def build_groups(items: list[dict]) -> list[dict]:
  groups = []
  for cat in CATEGORY_ORDER:
    subset = [it for it in items if it["category"] == cat]
    c = CATEGORIES[cat]
    groups.append({
      "category": cat,
      "name": c["name"],
      "count": len(subset),
      "bytes": sum(it["size"] for it in subset),
      "paths": c["paths"],
      "role": c["role"],
      "effect": c["effect"],
      "risk": c["risk"],
      "rule": c["rule"],
      "dirs": sorted({os.path.dirname(it["path"]) for it in subset})[:4],
    })
  return groups


def scan() -> dict:
  """只读扫描。受保护项在生成结果之前就被排除，绝不会出现在清单里。"""
  t0 = time.time()
  items, protected_hits = [], 0
  for cat, collect in COLLECTORS:
    try:
      paths = collect()
    except OSError:
      paths = []
    for p in paths:
      if is_protected(p, cat):
        protected_hits += 1
        continue
      item = _make_item(p, cat)
      if item:
        items.append(item)
  items.sort(key=lambda it: (CATEGORY_ORDER.index(it["category"]), -it["mtime"]))
  return {
    "ts": time.time(),
    "stamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "elapsed": round(time.time() - t0, 2),
    "items": items,
    "groups": build_groups(items),
    "protected_hits": protected_hits,
    "selfcheck": selftest(),
  }


# ---------------------------------------------------------------------------
# 格式化 / 状态文件
# ---------------------------------------------------------------------------
def fmt_size(value) -> str:
  try:
    n = int(value or 0)
  except (TypeError, ValueError):
    return "未知"
  if n >= 1024 * 1024:
    return f"{n / 1024 / 1024:.1f} MiB"
  return f"{n / 1024:.0f} KiB"


def fmt_time(ts) -> str:
  try:
    return time.strftime("%m-%d %H:%M", time.localtime(float(ts)))
  except (TypeError, ValueError, OSError):
    return "--"


def _atomic_write(path: str, text: str) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = path + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    f.write(text)
  os.replace(tmp, path)


def save_json(path: str, data) -> None:
  _atomic_write(path, json.dumps(data, ensure_ascii=False))


def load_json(path: str, default):
  try:
    with open(path, "r", encoding="utf-8") as f:
      return json.load(f)
  except (OSError, ValueError):
    return default


DEFAULT_SETTINGS = {"auto": False, "interval_hours": 6, "last_scan": 0}


def load_settings() -> dict:
  raw = load_json(SETTINGS_FILE, {})
  out = dict(DEFAULT_SETTINGS)
  if isinstance(raw, dict):
    out["auto"] = bool(raw.get("auto", False))
    try:
      hours = int(raw.get("interval_hours", 6))
    except (TypeError, ValueError):
      hours = 6
    out["interval_hours"] = hours if hours in (6, 12, 24) else 6
    try:
      out["last_scan"] = float(raw.get("last_scan", 0))
    except (TypeError, ValueError):
      out["last_scan"] = 0.0
  return out


def save_settings(data: dict) -> dict:
  clean = load_settings()
  clean.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
  save_json(SETTINGS_FILE, clean)
  return clean


def build_report(result: dict) -> str:
  sc = result.get("selfcheck") or {}
  lines = [
    "设备垃圾扫描清单",
    "扫描时间：" + str(result.get("stamp", "")),
    "规则：超过 %d 天 / 每类保留最近 %d 份 / 跳过最近 %d 分钟活跃文件" % (OLDER_THAN_DAYS, KEEP_RECENT, ACTIVE_MINUTES),
    "保护自检：%s（保护样例 %d 项，可删样例 %d 项）" % (
      "通过" if sc.get("ok") else "未通过", sc.get("protected_total", 0), sc.get("deletable_total", 0)),
    "扫描阶段排除的受保护项：%d" % int(result.get("protected_hits") or 0),
    "",
    "== 分类汇总（可删类别）==",
  ]
  for g in result.get("groups", []):
    lines.append("[%s] %s · %d 项 · %s · 风险 %s" % (
      g["category"], g["name"], g["count"], fmt_size(g["bytes"]), g["risk"]))
    lines.append("  作用：%s" % g["role"])
    lines.append("  删除影响：%s" % g["effect"])
    lines.append("  规则：%s" % g["rule"])
  lines.append("")
  lines.append("== 永不扫描 / 永不列出的路径 ==")
  for p in PROTECT_EXACT:
    lines.append("  精确保护 %s —— %s" % (p, PROTECT_REASONS.get(p, "")))
  for p in PROTECT_PREFIX:
    lines.append("  前缀保护 %s —— %s" % (p, PROTECT_PREFIX_REASONS.get(p, "")))
  lines.append("")
  lines.append("== 文件清单（共 %d 项）==" % len(result.get("items", [])))
  for it in result.get("items", []):
    lines.append("%s | %s | %s | %s | 作用：%s | 删除影响：%s | 风险：%s" % (
      it["category"], fmt_size(it["size"]), fmt_time(it["mtime"]), it["path"],
      it["role"], it["effect"], it["risk"]))
  return "\n".join(lines) + "\n"


def save_scan(result: dict) -> str:
  save_json(SCAN_FILE, result)
  report = build_report(result)
  _atomic_write(REPORT_FILE, report)
  save_settings({"last_scan": result.get("ts") or time.time()})
  return REPORT_FILE


def load_scan() -> dict | None:
  data = load_json(SCAN_FILE, None)
  return data if isinstance(data, dict) and isinstance(data.get("items"), list) else None


# ---------------------------------------------------------------------------
# 删除：默认移入隔离区，可恢复；彻底删除与恢复都要用户在界面二次确认
# ---------------------------------------------------------------------------
def load_manifest() -> list[dict]:
  data = load_json(MANIFEST_FILE, [])
  return data if isinstance(data, list) else []


def save_manifest(entries: list[dict]) -> None:
  save_json(MANIFEST_FILE, entries)


def trash_stats() -> dict:
  count, total = 0, 0
  if os.path.isdir(TRASH_ROOT):
    for dirpath, _, filenames in os.walk(TRASH_ROOT):
      for name in filenames:
        try:
          st = os.lstat(os.path.join(dirpath, name))
        except OSError:
          continue
        if stat.S_ISREG(st.st_mode):
          count += 1
          total += st.st_size
  return {"count": count, "bytes": total, "manifest": len(load_manifest())}


def trash_items(paths, dry_run: bool = False) -> dict:
  """把「本次扫描清单」里选中的文件移入隔离区。

  白名单来自 SCAN_FILE：不在清单里的路径一律拒绝，杜绝任意路径删除。
  """
  data = load_scan() or {}
  allowed = {it["path"]: it for it in data.get("items", [])}
  stamp = time.strftime("%Y%m%d_%H%M%S")
  trash_dir = os.path.join(TRASH_ROOT, stamp)
  moved, skipped, failed = [], [], []
  aborted = ""

  for p in paths:
    it = allowed.get(p)
    if it is None:
      skipped.append((p, "不在本次扫描清单"))
      continue
    cat = it["category"]
    if is_protected(p, cat):
      skipped.append((p, "命中保护规则"))
      continue
    try:
      st = os.lstat(p)
    except OSError:
      skipped.append((p, "文件已不存在"))
      continue
    if not stat.S_ISREG(st.st_mode):
      skipped.append((p, "不是普通文件或为符号链接"))
      continue
    if _is_recent(st.st_mtime):
      skipped.append((p, "最近 5 分钟仍在写"))
      continue
    dest = os.path.join(trash_dir, p.lstrip("/"))
    if not os.path.abspath(dest).startswith(os.path.abspath(TRASH_ROOT) + os.sep):
      skipped.append((p, "隔离区路径越界"))
      continue
    if dry_run:
      moved.append({"origin": p, "dest": dest, "size": st.st_size, "category": cat})
      continue
    try:
      os.makedirs(os.path.dirname(dest), exist_ok=True)
      shutil.move(p, dest)
    except OSError as exc:
      failed.append((p, str(exc)))
      continue
    moved.append({"origin": p, "dest": dest, "size": st.st_size, "category": cat, "at": stamp})
    if len(moved) % BATCH_SIZE == 0:
      missing = verify_keep()
      if missing:
        aborted = "关键文件缺失，已中止：" + ", ".join(missing)
        break

  if not dry_run and moved:
    save_manifest(load_manifest() + moved)
  missing_final = verify_keep()
  return {
    "dry_run": dry_run,
    "trash_dir": trash_dir,
    "moved": moved,
    "skipped": skipped,
    "failed": failed,
    "aborted": aborted,
    "missing_keep": missing_final,
  }


def restore_trash() -> dict:
  """把隔离区文件按原路径放回；原路径已有文件时跳过，不覆盖。"""
  entries = load_manifest()
  restored, skipped = [], []
  for e in entries:
    origin, dest = e.get("origin"), e.get("dest")
    if not origin or not dest:
      continue
    if os.path.exists(origin):
      skipped.append((origin, "原路径已有文件"))
      continue
    if not os.path.isfile(dest):
      skipped.append((dest, "隔离区文件不存在"))
      continue
    try:
      os.makedirs(os.path.dirname(origin), exist_ok=True)
      shutil.move(dest, origin)
      restored.append(origin)
    except OSError as exc:
      skipped.append((dest, str(exc)))
  if restored:
    done = set(restored)
    save_manifest([e for e in entries if e.get("origin") not in done])
  return {"restored": restored, "skipped": skipped}


def purge_trash() -> dict:
  """彻底清空隔离区（逐文件删除，不使用目录递归删除）。"""
  removed, failed = [], []
  for dirpath, _, filenames in os.walk(TRASH_ROOT):
    for name in filenames:
      p = os.path.join(dirpath, name)
      try:
        os.unlink(p)
        removed.append(p)
      except OSError as exc:
        failed.append((p, str(exc)))
  save_manifest([])
  return {"removed": removed, "failed": failed}


# ---------------------------------------------------------------------------
# 保护规则说明：界面从这里生成，保证「写的」与「做的」是同一份规则
# ---------------------------------------------------------------------------
def rule_sections() -> list[tuple[str, str]]:
  out = []
  out.append((
    "保护规则 1 · 总原则：五道闸",
    "<br>".join([
      "1 · 扫描默认只出清单，绝不删除任何文件。",
      "2 · 删除只接受「本次扫描清单里、且被勾选」的路径，不在清单里的一律拒绝。",
      "3 · 默认删除 = 移入隔离区 /data/carrot_trash/，可一键恢复；彻底删除必须单独二次确认。",
      "4 · 逐项复验：保护名单 / 严格文件名 / 普通文件非符号链接 / 跳过最近 5 分钟活跃文件 / 超过 14 天 / 每类保留最近 10 份。",
      "5 · 逐项处理，每 10 项核验一次关键文件仍在；发现缺失立即中止。",
      "6 · 行驶中（onroad）禁止删除，只允许只读扫描。",
    ]),
  ))
  out.append((
    "保护规则 2 · 扫描范围：只有这 7 类",
    "<br>".join([
      "只枚举下列 7 类确定形态的文件。其它任何目录、任何文件名都不会出现在清单里。",
      "完整清单导出到 " + REPORT_FILE + "（每次扫描自动刷新）。",
      "受保护项在扫描阶段就被排除，统计里会显示「扫描阶段排除的受保护项」数量。",
    ]),
  ))
  for cat in CATEGORY_ORDER:
    c = CATEGORIES[cat]
    out.append((
      "可删 %s · %s" % (cat, c["name"]),
      "<br>".join([
        "路径：" + "、".join(c["paths"]),
        "作用：" + c["role"],
        "删除影响：" + c["effect"],
        "风险：" + c["risk"],
        "保留规则：" + c["rule"],
      ]),
    ))
  out.append((
    "保护规则 3 · 永不扫描（精确路径 %d 项）" % len(PROTECT_EXACT),
    "<br>".join("%s —— %s" % (p, PROTECT_REASONS.get(p, "")) for p in PROTECT_EXACT),
  ))
  out.append((
    "保护规则 4 · 永不扫描（路径前缀 %d 项）" % len(PROTECT_PREFIX),
    "<br>".join("%s —— %s" % (p, PROTECT_PREFIX_REASONS.get(p, "")) for p in PROTECT_PREFIX),
  ))
  out.append((
    "保护规则 5 · 关键文件核验 %d 项" % len(VERIFY_KEEP),
    "<br>".join(VERIFY_KEEP) + "<br><br>任一缺失立即中止删除，界面会显示「关键文件核验」结果。",
  ))
  out.append((
    "保护规则 6 · 为什么不会列出重要文件",
    "<br>".join([
      "扫描器只进入 7 类目录，且文件名必须严格匹配（例如 swaglog.纯数字、YYYYMMDD-HHMMSS.mp4、tmp_pack_ 前缀）。",
      "每个候选在写入结果前都要过 is_protected()：精确名单、前缀名单、git 有效对象包后缀三重拦截。",
      "开机脚本 /data/continue.sh、参数 /data/params/、源码与 .git、大模型 /data/media/0/carrot/、备份 /data/carrot_backup/、隔离区 /data/carrot_trash/ 全部命中保护，根本不会进入列表。",
      "每次扫描还会跑一次保护名单自检：保护样例必须全部拦住、可删样例必须全部放行，结果显示在「保护自检」一行。",
    ]),
  ))
  return out
