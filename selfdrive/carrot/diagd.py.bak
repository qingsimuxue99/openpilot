#!/usr/local/venv/bin/python3
# -*- coding: utf-8 -*-
"""
c3 设备诊断守护 (diagd)  ——  AGNOS 适配版
============================================
后台常驻采集设备所有错误/故障/硬件异常/版本偏差，结构化输出到
/data/c3_toolbox/diag/，带轮转清理。随设备开机自启。
网页工具箱 (c3_toolbox) 数据目录正是 /data/c3_toolbox，故产出天然可被读取/下载/分享。

!!! AGNOS 的 logcat 已损坏 (/usr/bin/logcat 缺 libbase.so.0，/system/bin/logcat 不存在)，
!!! 本守护**不依赖 logcat**，改用以下真实可用的错误源：
  1. dmesg        内核/硬件错误（panda/CAN/thermal/usb/ext4/selinux/oom/watchdog）
  2. openpilot 进程存活  manager.py / pandad / carrot_man 崩溃或重启
  3. qlog 历史    最近已完成 route 的 qlog.zst 提取 error 事件 + 非空 alertText
  4. git dirty     未提交改动（过滤 .bak/prebuilt 等噪音），解释"为何和别人不一样"
  5. 硬件          温度超阈 / 存储不足
  6. 版本偏差      panda 固件戳 != git HEAD 短哈希

去重：diag_state.json 记录上次状态，仅在"新出现/状态翻转"时写 errors.log，
      避免健康设备上每周期刷屏。

输出：errors.log / diag_YYYYMMDD.json / latest.json / report.txt / daemon.log /
      routes_processed.json(去重) / diag_state.json(状态)

用法：
  python diagd.py            # 守护模式
  python diagd.py --once     # 采集一次并退出（测试）
  python diagd.py --report   # 重生成 report.txt
  python diagd.py --clean    # 强制清理
  python diagd.py --status   # pid / 上次采集时间
"""

import os
import sys
import time
import json
import subprocess
import re
import glob
import datetime
import fcntl

sys.path.insert(0, "/data/openpilot")

# ------------------------- 配置 -------------------------
DIAG_DIR = "/data/c3_toolbox/diag"
ERRORS_LOG = os.path.join(DIAG_DIR, "errors.log")
LATEST_JSON = os.path.join(DIAG_DIR, "latest.json")
DAEMON_LOG = os.path.join(DIAG_DIR, "daemon.log")
PID_FILE = os.path.join(DIAG_DIR, "diagd.pid")
STATE_FILE = os.path.join(DIAG_DIR, "diag_state.json")
ROUTES_STATE = os.path.join(DIAG_DIR, "routes_processed.json")
OPENPILOT_DIR = "/data/openpilot"
PARAMS_D = "/data/params/d"
REALDATA_DIR = "/data/media/0/realdata"

INTERVAL = 60
TEMP_WARN = 70.0
TEMP_CRIT = 85.0
STORAGE_WARN_GB = 1.0
MAX_ERRORS_SIZE = 5 * 1024 * 1024
MAX_ERRORS_BACKUP = 5
DAILY_KEEP_DAYS = 30
MAX_TOTAL_BYTES = 100 * 1024 * 1024

# git dirty 过滤（用户铁律排除项，避免噪音）
DIRTY_EXCLUDE = [".bak", "prebuilt", "nav_params.json", "screen_cap",
                 "mjpeg_server", "debug", ".bak_", "params_keys"]

KEY_PROCS = {
    "manager.py": "python3 ./manager.py",
    "pandad": "selfdrive.pandad.pandad",
    "carrot_man": "selfdrive.carrot.carrot_man",
}

DMESG_NOISE = ["hdd_is_rcpi_applicable", "__hdd_ioctl", "mac addr is different",
               "wlan:", "cfg80211", "WCND", "wcnss",
               "usb 1-1: new high-speed USB", "usb 1-1: new full-speed USB",
               "usb 1-1: new SuperSpeed USB"]
DMESG_FAULT = ["error", "fail", "panic", "watchdog", "cannot", "denied",
               "oom", "exception", "fatal", "can:", "panda", "ext4",
               "selinux", "timeout", "crash", "reset", "thermal",
               "over-temperature", "temperature", "critical",
               "under-voltage", "over-current", "over-temp"]

# ------------------------- 工具 -------------------------
def now():
    return datetime.datetime.now()

def log(msg):
    print("[%s] %s" % (now().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)

def run(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout, p.stderr, p.returncode
    except Exception as e:
        return "", str(e), -1

def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass

# ------------------------- 采集 -------------------------
def read_dmesg_incremental(last_sec):
    out, _, _ = run(["dmesg"], timeout=15)
    if not out:
        return [], last_sec
    new = []
    cur_max = last_sec
    for line in out.splitlines():
        m = re.match(r'^\[(\d+\.\d+)\]\s+(.*)$', line)
        if not m:
            if new:
                new[-1] += "\n" + line
            continue
        sec = float(m.group(1))
        if last_sec is not None and sec <= last_sec:
            continue
        text = m.group(2)
        low = text.lower()
        if any(n in text for n in DMESG_NOISE):
            continue
        if not any(k in low for k in DMESG_FAULT):
            continue
        new.append("[dmesg %.3f] %s" % (sec, text))
        if cur_max is None or sec > cur_max:
            cur_max = sec
    return new, cur_max

def check_processes(state):
    faults = []
    for name, pat in KEY_PROCS.items():
        out, _, _ = run(["pgrep", "-f", pat], timeout=10)
        pids = [p for p in out.split() if p.strip().isdigit()]
        if not pids:
            faults.append("%s 进程未运行(可能崩溃)" % name)
            state["last_%s_pid" % name] = None
        else:
            cur = pids[0]
            prev = state.get("last_%s_pid" % name)
            if prev is not None and prev != cur:
                faults.append("%s 进程重启 (旧PID %s -> 新PID %s)" % (name, prev, cur))
            state["last_%s_pid" % name] = cur
    return faults

def extract_route_alerts():
    try:
        from openpilot.tools.lib.logreader import LogReader
    except Exception as e:
        return ["[qlog] LogReader 导入失败: %s" % e]
    state = load_json(ROUTES_STATE, {})
    routes = sorted(glob.glob(os.path.join(REALDATA_DIR, "*/")))
    tnow = time.time()
    lines = []
    for r in routes[-6:]:
        name = os.path.basename(r.rstrip("/"))
        if name in state:
            continue
        try:
            if tnow - os.path.getmtime(r) < 300:
                continue
        except Exception:
            continue
        qlog = None
        for fn in ("qlog", "qlog.zst", "qlog.bz2", "qlog.gz", "rlog", "rlog.zst"):
            if os.path.isfile(os.path.join(r, fn)):
                qlog = os.path.join(r, fn)
                break
        if not qlog:
            state[name] = tnow
            continue
        try:
            lr = LogReader(qlog)
            for m in lr:
                w = m.which()
                if w == "error":
                    lines.append("[ROUTE %s] error: %s" % (name, str(m)[:300]))
                elif w == "controlsState":
                    try:
                        cs = m.controlsState
                        t1 = getattr(cs, "alertText1", "") or ""
                        t2 = getattr(cs, "alertText2", "") or ""
                        # alertId 是枚举，比 alertText 更可靠地标识"有报警"
                        aid = getattr(cs, "alertId", None)
                        aid_s = ""
                        if aid is not None:
                            try:
                                v = aid.value if hasattr(aid, "value") else int(aid)
                                if v:
                                    aid_s = "alertId=%s" % v
                            except Exception:
                                aid_s = "alertId=%s" % aid
                        tag = " ".join([x for x in (t1, t2, aid_s) if x]).strip()
                        if tag:
                            lines.append("[ROUTE %s] alert: %s" % (name, tag))
                    except Exception:
                        pass
        except Exception as e:
            lines.append("[ROUTE %s] 解析失败: %s" % (name, e))
        state[name] = tnow
    save_json(ROUTES_STATE, state)
    return lines

def get_thermal():
    temps = []
    try:
        for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            with open(z) as f:
                v = f.read().strip()
                if v.isdigit():
                    temps.append(int(v) / 1000.0)
    except Exception:
        pass
    if not temps:
        return None, 0
    return max(temps), len(temps)

def get_storage():
    out, _, _ = run(["df", "-P", "/data"], timeout=10)
    for line in out.splitlines():
        if line.startswith("/"):
            parts = line.split()
            if len(parts) >= 4 and parts[3].isdigit():
                return int(parts[3]) * 1024
    return None

def get_git():
    branch, _, _ = run(["git", "-C", OPENPILOT_DIR, "branch", "--show-current"])
    head, _, _ = run(["git", "-C", OPENPILOT_DIR, "rev-parse", "HEAD"])
    dirty_out, _, _ = run(["git", "-C", OPENPILOT_DIR, "status", "--porcelain"])
    dirty = [l.strip() for l in dirty_out.splitlines() if l.strip()]
    dirty = [d for d in dirty if not any(x in d for x in DIRTY_EXCLUDE)]
    return {"branch": branch.strip(), "head": head.strip(),
            "short": head.strip()[:8], "dirty": dirty}

def get_panda_fw():
    for name in ("PandaFirmware", "PandaFirmwareHex"):
        p = os.path.join(PARAMS_D, name)
        if os.path.isfile(p):
            try:
                return open(p).read().strip()
            except Exception:
                pass
    return ""

def get_device_info():
    dongle = ""
    p = os.path.join(PARAMS_D, "DongleId")
    if os.path.isfile(p):
        try:
            dongle = open(p).read().strip()
        except Exception:
            pass
    if not dongle:
        dongle, _, _ = run(["hostname"], timeout=5)
        dongle = dongle.strip()
    version = ""
    vp = os.path.join(OPENPILOT_DIR, "VERSION")
    if os.path.isfile(vp):
        try:
            version = open(vp).read().strip()
        except Exception:
            pass
    return {"dongle_id": dongle, "version": version, "diag_dir": DIAG_DIR}

def psutil_boot():
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    return int(line.split()[1])
    except Exception:
        pass
    return int(time.time()) - 1

def build_snapshot(state):
    tmax, tcnt = get_thermal()
    free = get_storage()
    g = get_git()
    fw = get_panda_fw()
    proc_missing = check_processes(state)
    return {
        "ts": now().strftime("%Y-%m-%d %H:%M:%S"),
        "uptime_s": int(time.time() - psutil_boot()),
        "thermal_max_c": round(tmax, 1) if tmax is not None else None,
        "thermal_zones": tcnt,
        "storage_free_gb": round(free / 1024.0 / 1024.0 / 1024.0, 2) if free else None,
        "git": g,
        "panda_fw": fw,
        "panda_fw_match": (bool(fw) and fw[:8].lower() == g["short"].lower()),
        "proc_missing": proc_missing,
    }

# ------------------------- 写入 -------------------------
def append_raw(lines, boot=False):
    if not lines and not boot:
        return
    os.makedirs(DIAG_DIR, exist_ok=True)
    with open(ERRORS_LOG, "a", encoding="utf-8") as f:
        if boot:
            dev = get_device_info()
            f.write("%s [BOOT] 诊断守护启动 设备:%s VERSION:%s\n" % (
                now().strftime("%Y-%m-%d %H:%M:%S"), dev.get("dongle_id"), dev.get("version")))
        for ln in lines:
            f.write("%s %s\n" % (now().strftime("%Y-%m-%d %H:%M:%S"), ln))

def record_hardware(snap, state):
    lines = []
    t = snap.get("thermal_max_c")
    if t is not None and t >= TEMP_WARN:
        if not state.get("temp_warn"):
            lvl = "E" if t >= TEMP_CRIT else "W"
            lines.append("[%s] thermal 设备温度过高 %.1f°C (阈值 %.0f/%.0f)" % (lvl, t, TEMP_WARN, TEMP_CRIT))
        state["temp_warn"] = True
    else:
        if state.get("temp_warn"):
            lines.append("[I] thermal 温度恢复正常 %.1f°C" % (t if t else 0))
        state["temp_warn"] = False
    free_gb = snap.get("storage_free_gb")
    if free_gb is not None and free_gb < STORAGE_WARN_GB:
        if not state.get("storage_warn"):
            lines.append("[E] storage /data 剩余空间不足 %.2fGB" % free_gb)
        state["storage_warn"] = True
    else:
        if state.get("storage_warn"):
            lines.append("[I] storage 空间恢复正常 %.2fGB" % (free_gb if free_gb else 0))
        state["storage_warn"] = False
    cur = set(snap.get("proc_missing", []))
    last = set(state.get("last_proc_missing", []))
    for p in sorted(cur - last):
        lines.append("[proc] 进程异常: %s" % p)
    state["last_proc_missing"] = sorted(cur)
    if lines:
        append_raw(lines)

def record_version(snap, state):
    g = snap.get("git", {})
    cur = set(g.get("dirty", []))
    last = set(state.get("last_dirty", []))
    new = sorted(cur - last)
    if new:
        append_raw(["[W] git 新增未提交改动(%d):" % len(new)] + ["    %s" % d for d in new][:30])
    state["last_dirty"] = sorted(cur)
    fw = snap.get("panda_fw", "")
    match = snap.get("panda_fw_match")
    if fw and g.get("short") and not match:
        if not state.get("panda_mismatch"):
            append_raw(["[E] version panda 固件戳(%s) != git HEAD(%s)，固件与代码不一致" % (fw[:8], g["short"])])
        state["panda_mismatch"] = True
    else:
        state["panda_mismatch"] = False

def write_latest(snap):
    try:
        with open(LATEST_JSON, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写 latest.json 失败: %s" % e)

def append_daily(snap, extra):
    day = now().strftime("%Y%m%d")
    path = os.path.join(DIAG_DIR, "diag_%s.json" % day)
    entry = {
        "ts": snap["ts"], "thermal_max_c": snap.get("thermal_max_c"),
        "storage_free_gb": snap.get("storage_free_gb"), "git": snap.get("git"),
        "panda_fw_match": snap.get("panda_fw_match"),
        "proc_missing": snap.get("proc_missing"), "extra_errors": len(extra),
    }
    os.makedirs(DIAG_DIR, exist_ok=True)
    data = []
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, list):
                data = []
        except Exception:
            data = []
    data.append(entry)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写每日汇总失败: %s" % e)

# ------------------------- 轮转 -------------------------
def rotate_errors():
    try:
        if not os.path.isfile(ERRORS_LOG) or os.path.getsize(ERRORS_LOG) < MAX_ERRORS_SIZE:
            return
        for i in range(MAX_ERRORS_BACKUP - 1, 0, -1):
            src = "%s.%d" % (ERRORS_LOG, i)
            dst = "%s.%d" % (ERRORS_LOG, i + 1)
            if os.path.isfile(src):
                os.replace(src, dst)
        os.replace(ERRORS_LOG, ERRORS_LOG + ".1")
        log("errors.log 已轮转")
    except Exception as e:
        log("轮转 errors.log 失败: %s" % e)

def clean_old():
    os.makedirs(DIAG_DIR, exist_ok=True)
    cutoff = now() - datetime.timedelta(days=DAILY_KEEP_DAYS)
    for p in glob.glob(os.path.join(DIAG_DIR, "diag_*.json")):
        try:
            if datetime.datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
                os.remove(p)
        except Exception:
            pass
    daily = sorted(glob.glob(os.path.join(DIAG_DIR, "diag_*.json")),
                   key=lambda x: os.path.getmtime(x))
    while daily:
        total = sum(os.path.getsize(x) for x in glob.glob(os.path.join(DIAG_DIR, "*"))
                    if os.path.isfile(x))
        if total <= MAX_TOTAL_BYTES or len(daily) <= 1:
            break
        oldest = daily.pop(0)
        try:
            os.remove(oldest)
        except Exception:
            break

# ------------------------- 报告 -------------------------
def gen_report():
    os.makedirs(DIAG_DIR, exist_ok=True)
    dev = get_device_info()
    snap = build_snapshot({})
    lines = []
    lines.append("=" * 60)
    lines.append("c3 设备诊断报告 (diagd)")
    lines.append("生成时间: %s" % now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("=" * 60)
    lines.append("设备: %s" % dev.get("dongle_id"))
    lines.append("VERSION: %s" % dev.get("version"))
    lines.append("分支: %s   提交: %s" % (snap["git"]["branch"], snap["git"]["short"]))
    lines.append("panda 固件戳: %s   与代码一致: %s" % (snap.get("panda_fw", "")[:8], snap.get("panda_fw_match")))
    lines.append("温度(最高): %s°C   剩余空间: %sGB" % (snap.get("thermal_max_c"), snap.get("storage_free_gb")))
    if snap.get("proc_missing"):
        lines.append("")
        lines.append("[!] 进程异常:")
        for p in snap["proc_missing"]:
            lines.append("    %s" % p)
    if snap["git"]["dirty"]:
        lines.append("")
        lines.append("[!] 工作区未提交改动 (%d):" % len(snap["git"]["dirty"]))
        for d in snap["git"]["dirty"][:30]:
            lines.append("    %s" % d)
    lines.append("")
    lines.append("-" * 60)
    lines.append("近期错误/故障流 (errors.log 末尾 200 行):")
    lines.append("-" * 60)
    if os.path.isfile(ERRORS_LOG):
        try:
            with open(ERRORS_LOG, encoding="utf-8", errors="replace") as f:
                tail = f.read().splitlines()[-200:]
            lines.extend(tail)
        except Exception as e:
            lines.append("(读取 errors.log 失败: %s)" % e)
    else:
        lines.append("(暂无错误记录)")
    report_path = os.path.join(DIAG_DIR, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return report_path

# ------------------------- 单例 -------------------------
def acquire_lock():
    os.makedirs(DIAG_DIR, exist_ok=True)
    try:
        fd = os.open(PID_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(PID_FILE, "w") as f:
            f.write(str(os.getpid()))
        return True
    except OSError:
        return False

# ------------------------- 主流程 -------------------------
def collect_once(state):
    snap = build_snapshot(state)
    dmesg_new, new_sec = read_dmesg_incremental(state.get("last_dmesg_sec"))
    state["last_dmesg_sec"] = new_sec
    route_lines = extract_route_alerts()
    all_new = dmesg_new + route_lines
    append_raw(all_new)
    record_hardware(snap, state)
    record_version(snap, state)
    write_latest(snap)
    append_daily(snap, all_new)
    return len(all_new)

def daemon_main():
    if not acquire_lock():
        log("已有实例在运行，退出")
        sys.exit(0)
    log("诊断守护启动")
    state = load_json(STATE_FILE, {})
    append_raw([], boot=True)
    try:
        while True:
            try:
                cnt = collect_once(state)
                save_json(STATE_FILE, state)
                rotate_errors()
                clean_old()
            except Exception as e:
                log("采集周期异常: %s" % e)
            time.sleep(INTERVAL)
    except KeyboardInterrupt:
        log("收到中断，退出")

def main():
    args = sys.argv[1:]
    if "--once" in args:
        os.makedirs(DIAG_DIR, exist_ok=True)
        state = load_json(STATE_FILE, {})
        cnt = collect_once(state)
        save_json(STATE_FILE, state)
        gen_report()
        log("单次采集完成，新增错误/故障 %d 条" % cnt)
        return
    if "--report" in args:
        p = gen_report()
        log("报告已生成: %s" % p)
        return
    if "--clean" in args:
        rotate_errors()
        clean_old()
        log("清理完成")
        return
    if "--status" in args:
        if os.path.isfile(PID_FILE):
            print("PID: %s" % open(PID_FILE).read().strip())
        else:
            print("未运行")
        if os.path.isfile(LATEST_JSON):
            print(open(LATEST_JSON).read())
        return
    daemon_main()

if __name__ == "__main__":
    main()
