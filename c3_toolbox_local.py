#!/usr/bin/env python3
"""
C3 设备工具箱 - 设备本地版 v2 (Carrotpilot cpv9-dev)
=====================================================
直接运行在 C3 设备上，浏览器访问 http://设备IP:5588 即可
"""

import json, os, sys, time, subprocess, urllib.request, tarfile, io, threading, traceback, re

try:
    from flask import Flask, request, jsonify, send_file, send_from_directory, Response
except ImportError:
    print("[!] 需要安装 flask: pip install flask")
    sys.exit(1)

app = Flask(__name__)


# 禁止浏览器缓存页面与接口，避免手机端停留在旧版 HTML（导致设备信息等内容显示不全）
@app.after_request
def no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


BASE_DIR = "/data/c3_toolbox"                 # 固定设备端数据目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录(放 HTML 用)
PARAMS_DIR = "/data/params/d"
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
AUTO_BACKUP_DIR = os.path.join(BASE_DIR, "auto_backup")
AUTO_BACKUP_FILE = os.path.join(AUTO_BACKUP_DIR, "auto_full_params.json")
LOG_FILE = os.path.join(BASE_DIR, "server.log")

# 工具箱版本与在线更新源
# 发布新版本：把新文件推到 GitHub 仓库的 c3-toolbox 分支，并更新 version.json 的 version
# ============= 在线更新配置（版本指针机制，彻底解耦）=============
# 三段式，全程 jsdelivr 域（国内可达）+ 具体 tag（不可变缓存），彻底绕开
# 分支/@latest 等"浮动引用"的强缓存问题，设备端代码永不需要修改：
#   1) 发现最新版本：jsdelivr 数据 API 实时返回 versions 列表，首个即最新 tag
#   2) 读该版本 version.json：用具体 tag @vX.Y.Z（不可变，稳定拿到 changelog/tarball）
#   3) 下载发布包：version.json 里的 tarball 指针（具体 tag，不可变，最新鲜）
# 发新版本只需：改 version.json(version/tag/tarball) + 打 tag 推送，设备自动发现。
REPO = "qingsimuxue99/openpilot"
VERSION = "1.0.41"
# 实时发现最新版本号的数据 API（属 jsdelivr 域，国内可达，不受 CDN 文件缓存影响）
JSDELIVR_DATA_API = "https://data.jsdelivr.com/v1/package/gh/%s" % REPO
# 读 version.json 的兜底源（当数据 API 不可用时，用浮动引用兜底；可能滞后但保证可用）
CHECK_MIRRORS = [
    "https://cdn.jsdelivr.net/gh/%s@latest/" % REPO,
    "https://raw.githubusercontent.com/%s/c3-toolbox/" % REPO,
]
# 兼容旧引用
UPDATE_BASE = CHECK_MIRRORS[0]
UPDATE_MIRRORS = CHECK_MIRRORS

# ============= 完整备份/恢复（/data/openpilot 整目录 tar 包）=============
OP_BK_PREFIX = "备份恢复包openpilot_backup_"
OP_BK_DIR = "/data"
# 后台任务状态（备份/恢复可能耗时数分钟，前端轮询获取进度）
OP_TASKS = {}

# 启动时确保目录存在
os.makedirs(BASE_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(AUTO_BACKUP_DIR, exist_ok=True)


# 设备端文件不存在时返回默认值
def read_file(path, default=""):
    try:
        if os.path.isfile(path):
            with open(path, 'r') as f:
                return f.read().strip()
    except:
        pass
    return default


def run_cmd(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except:
        return ""


def safe_key(key):
    return key.replace("/", "").replace("\\", "").replace("..", "")


def get_device_info():
    """完整获取设备信息"""
    info = {}
    # 参数文件
    for k, label in [('DongleId','dongle_id'), ('Version','version'), ('GitBranch','branch'),
                      ('GitCommit','commit'), ('CarName','car_name')]:
        v = read_file(os.path.join(PARAMS_DIR, k))
        if v:
            info[label] = v
    # 设备型号
    model = read_file("/sys/firmware/devicetree/base/model").replace('\x00','').strip()
    if model:
        info['model'] = model
    # 运行时间
    uptime = run_cmd("uptime -p 2>/dev/null || uptime")
    if uptime:
        info['uptime'] = uptime
    # 内存
    mem = run_cmd("free -m | awk '/Mem:/{print $2\"MB total / \"$3\"MB used / \"$4\"MB free\"}'")
    if mem:
        info['memory'] = mem
    # CPU 温度
    temp = run_cmd("cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null")
    if temp:
        info['cpu_temp'] = f"{int(temp)/1000:.1f}°C"
    # GPU 温度
    gpu_temp = run_cmd("cat /sys/class/thermal/thermal_zone1/temp 2>/dev/null")
    if gpu_temp:
        info['gpu_temp'] = f"{int(gpu_temp)/1000:.1f}°C"
    # 磁盘
    disk = run_cmd("df -h /data | awk 'NR==2{print $3\" / \"$2\" (\"$5\" used)\"}'")
    if disk:
        info['disk'] = disk
    # IP 地址
    ip = run_cmd("hostname -I 2>/dev/null | awk '{print $1}'")
    if ip:
        info['ip'] = ip
    # 进程状态
    mgr = run_cmd("pgrep -c manager.py 2>/dev/null")
    info['manager_running'] = int(mgr) > 0 if mgr else False
    # 启动次数
    bc = read_file(os.path.join(PARAMS_DIR, 'BootCount'))
    if bc:
        info['boot_count'] = bc
    # 摄像头类型
    ct = read_file(os.path.join(PARAMS_DIR, 'CameraType'))
    if ct:
        info['camera_type'] = ct
    # 系统分区大小
    sys_disk = run_cmd("df -h / | awk 'NR==2{print $3\" / \"$2\" (\"$5\" used)\"}'")
    if sys_disk:
        info['system_disk'] = sys_disk
    # CPU 核心数
    cpu_cores = run_cmd("nproc 2>/dev/null")
    if cpu_cores:
        info['cpu_cores'] = cpu_cores.strip() + ' 核'
    # CPU 型号
    cpu_model = run_cmd("cat /proc/cpuinfo | grep 'Hardware\\|model name' | head -1 | cut -d: -f2 | xargs 2>/dev/null")
    if cpu_model:
        info['cpu_model'] = cpu_model.strip()
    # 电池/BMS状态
    battery_cap = run_cmd("cat /sys/class/power_supply/battery/capacity 2>/dev/null")
    if battery_cap:
        info['battery'] = battery_cap.strip() + '%'
    else:
        # 尝试其他电池路径
        battery_cap2 = run_cmd("cat /sys/class/power_supply/BAT0/capacity 2>/dev/null")
        if battery_cap2:
            info['battery'] = battery_cap2.strip() + '%'
    # 充电状态
    charging = run_cmd("cat /sys/class/power_supply/battery/status 2>/dev/null")
    if charging:
        info['charging'] = charging.strip()
    # BMS 温度
    bms_temp = run_cmd("cat /sys/class/power_supply/battery/temp 2>/dev/null")
    if bms_temp:
        try:
            info['bms_temp'] = f"{int(bms_temp)/10:.1f}°C"
        except:
            pass
    # WiFi 状态
    wifi_ssid = run_cmd("iwgetid -r 2>/dev/null || wpa_cli -i wlan0 status 2>/dev/null | grep '^ssid=' | cut -d= -f2")
    if wifi_ssid:
        info['wifi'] = wifi_ssid.strip()
    # 联网状态
    net_test = run_cmd("ping -c 1 -W 2 8.8.8.8 2>/dev/null | grep '1 received'")
    info['network'] = '正常' if net_test else '异常'
    # 屏幕亮度
    brightness = run_cmd("cat /sys/class/backlight/backlight/brightness 2>/dev/null")
    if brightness:
        max_bright = run_cmd("cat /sys/class/backlight/backlight/max_brightness 2>/dev/null")
        if max_bright:
            try:
                pct = int(int(brightness)/int(max_bright)*100)
                info['brightness'] = f"{brightness.strip()}/{max_bright.strip()} ({pct}%)"
            except:
                info['brightness'] = brightness.strip()
        else:
            info['brightness'] = brightness.strip()
    # 空间温度 (机箱/环境)
    ambient_temp = run_cmd("cat /sys/class/thermal/thermal_zone2/temp 2>/dev/null")
    if ambient_temp:
        try:
            info['ambient_temp'] = f"{int(ambient_temp)/1000:.1f}°C"
        except:
            pass
    # 负载
    load_avg = run_cmd("cat /proc/loadavg | awk '{print $1\" \"$2\" \"$3}'")
    if load_avg:
        info['load_avg'] = load_avg.strip()
    # 进程数
    proc_count = run_cmd("ps -e | wc -l")
    if proc_count:
        info['proc_count'] = proc_count.strip()
    return info


# ============= 在线更新 =============

def cmp_version(a, b):
    def parse(v):
        try:
            return [int(x) for x in str(v).split('.')]
        except Exception:
            return [0]
    pa, pb = parse(a), parse(b)
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return (pa > pb) - (pa < pb)


def _fetch_bytes(url, timeout=20):
    req = urllib.request.Request(url, headers={'User-Agent': 'c3-toolbox-update'})
    return urllib.request.urlopen(req, timeout=timeout).read()


def try_fetch_bytes(suffix, timeout=30):
    """遍历镜像源拉取文件字节，逐源回退；全部失败抛最后一个异常"""
    last_err = None
    for base in UPDATE_MIRRORS:
        try:
            return _fetch_bytes(base.rstrip('/') + '/' + suffix, timeout)
        except Exception as e:
            last_err = e
    raise last_err


def download_file(suffix, dest, timeout=30):
    """从更新源镜像拉取文件，逐镜像回退；成功写入 dest"""
    data = try_fetch_bytes(suffix, timeout)
    tmp = dest + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dest)


def fetch_text(suffix, timeout=20):
    """从更新源镜像拉取文本（如 version.json），逐镜像回退"""
    return try_fetch_bytes(suffix, timeout).decode('utf-8')


def fetch_bytes_from_urls(urls, timeout=60):
    """按给定的完整 URL 列表逐个尝试下载，返回首个成功的字节；全部失败抛最后一个异常"""
    last_err = None
    for u in urls:
        if not u:
            continue
        try:
            return _fetch_bytes(u, timeout)
        except Exception as e:
            last_err = e
    if last_err:
        raise last_err
    raise RuntimeError('无可用下载地址')


def resolve_tarball_urls(remote):
    """由远程 version.json（版本指针）解析出发布包下载地址，按优先级返回：
    1) version.json 显式给出的 tarball 完整 URL（最高优先）
    2) 按 tag 拼出的 jsdelivr（不可变缓存）+ raw 具体 tag 地址
    3) 兜底：CHECK_MIRRORS 下的 release 路径
    """
    urls = []
    tb = remote.get('tarball')
    if tb:
        urls.append(tb)
    tag = remote.get('tag')
    if tag:
        urls.append("https://cdn.jsdelivr.net/gh/%s@%s/release/c3_toolbox.tar.gz" % (REPO, tag))
        urls.append("https://raw.githubusercontent.com/%s/%s/release/c3_toolbox.tar.gz" % (REPO, tag))
    for base in CHECK_MIRRORS:
        urls.append(base.rstrip('/') + '/release/c3_toolbox.tar.gz')
    return urls


def _semver_key(v):
    """把 'v1.0.12' / '1.0.12' 解析为可比较的 (major,minor,patch) 元组"""
    m = re.match(r'v?(\d+)\.(\d+)\.(\d+)', str(v))
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def discover_latest_version_github():
    """通过 GitHub API 实时发现最新版本号（tag）。
    GitHub 在 git push 新 tag 后立即可见，不受 jsdelivr 数据 API 索引延迟影响（根治‘发布后点更新认不到新版’的问题）。
    返回如 '1.0.12'；失败返回 None。"""
    try:
        url = "https://api.github.com/repos/%s/tags?per_page=100" % REPO
        data = json.loads(_fetch_bytes(url, 15).decode('utf-8'))
        tags = [t.get('name') for t in data if isinstance(t, dict) and t.get('name')]
        # 只保留 vX.Y.Z / X.Y.Z 形态，按语义版本降序取最新
        cand = [t for t in tags if re.match(r'^v?\d+\.\d+\.\d+$', t)]
        if cand:
            cand.sort(key=_semver_key, reverse=True)
            return cand[0].lstrip('v')
    except Exception:
        pass
    return None


def discover_latest_version():
    """发现最新版本号。优先 GitHub API（实时，推送 tag 后立即可见）；
    失败回退 jsdelivr 数据 API（国内可达兜底）。返回如 '1.0.12'；失败返回 None。"""
    v = discover_latest_version_github()
    if v:
        return v
    try:
        data = json.loads(_fetch_bytes(JSDELIVR_DATA_API, 15).decode('utf-8'))
        versions = data.get('versions') or []
        # jsdelivr 按 semver 降序排列，首个即最新；兼容「字符串」与「对象」两种返回形态
        if versions:
            v0 = versions[0]
            if isinstance(v0, dict):
                return str(v0.get('version'))
            return str(v0)
    except Exception:
        pass
    return None


def fetch_remote_meta():
    """获取远程版本信息（version.json 内容）。策略：
    1) 数据 API 实时发现最新版本 → 用具体 tag @vX.Y.Z 读该版本 version.json（不可变、稳定）
    2) 回退：@latest / raw 分支（浮动引用，可能滞后但兜底可用）
    返回解析后的 dict；全部失败抛异常。"""
    candidates = []
    ver = discover_latest_version()
    if ver:
        tag = ver if str(ver).startswith('v') else ('v' + str(ver))
        candidates.append("https://cdn.jsdelivr.net/gh/%s@%s/version.json" % (REPO, tag))
    for base in CHECK_MIRRORS:
        candidates.append(base.rstrip('/') + '/version.json')
    last_err = None
    for url in candidates:
        try:
            return json.loads(_fetch_bytes(url, 15).decode('utf-8'))
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError('无法获取远程版本信息')


def schedule_restart():
    """下载完成后，延迟杀掉旧端口并启动新实例，避免端口抢占"""
    script = os.path.abspath(__file__)
    cmd = "(sleep 2; fuser -k 5588/tcp 2>/dev/null; sleep 1; cd %s; setsid %s %s >> %s 2>&1 < /dev/null &)" % (
        BASE_DIR, sys.executable, script, LOG_FILE)
    subprocess.Popen(cmd, shell=True, start_new_session=True)
    time.sleep(0.3)
    os._exit(0)


@app.route('/api/version')
def api_version():
    return jsonify({'version': VERSION, 'update_base': UPDATE_BASE})


@app.route('/api/check_update')
def api_check_update():
    try:
        remote = fetch_remote_meta()
        remote_ver = remote.get('version', '0')
        return jsonify({
            'local_version': VERSION,
            'remote_version': remote_ver,
            'update_available': cmp_version(remote_ver, VERSION) > 0,
            'changelog': remote.get('changelog', ''),
            'error': '',
        })
    except Exception as e:
        return jsonify({'local_version': VERSION, 'remote_version': '', 'update_available': False, 'changelog': '', 'error': str(e)})


def _delayed_restart(delay=1.5):
    """延迟重启，确保 /api/update 的成功响应已发回前端（避免 os._exit 截断响应）"""
    time.sleep(delay)
    schedule_restart()


@app.route('/api/update', methods=['POST'])
def api_update():
    try:
        # 版本指针：先读远程 version.json（数据 API 发现最新版 → 具体 tag 读取），
        # 由它指定要下载哪个发布包（tag/tarball）
        remote = fetch_remote_meta()
        urls = resolve_tarball_urls(remote)
        # 下载发布包并解压到 BASE_DIR（原子替换，避免 py/html 版本错配）
        data = fetch_bytes_from_urls(urls, 60)
        with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as tf:
            # Python 3.12+ 要求显式指定 filter，否则拒绝解压（PEP 706）；3.11 无该参数
            if sys.version_info >= (3, 12):
                tf.extractall(BASE_DIR, filter='data')
            else:
                tf.extractall(BASE_DIR)
        # 先返回成功响应，再后台延迟重启，避免 os._exit 截断 HTTP 响应导致前端误报“更新失败”
        threading.Thread(target=_delayed_restart, daemon=True).start()
        return jsonify({'success': True, 'message': '更新完成，正在重启服务...'})
    except Exception as e:
        app.logger.error('在线更新失败: %s', traceback.format_exc())
        return jsonify({'success': False, 'message': '更新失败 [%s]: %s' % (type(e).__name__, e)})


# ============= 路由 =============

@app.route('/')
def index():
    p = os.path.join(SCRIPT_DIR, 'c3_toolbox.html')
    if os.path.isfile(p):
        return send_file(p)
    return "<h1>c3_toolbox.html 不存在</h1>"


@app.route('/hud')
def hud_page():
    p = os.path.join(SCRIPT_DIR, 'hud.html')
    if os.path.isfile(p):
        return send_file(p)
    return "<h1>hud.html 不存在</h1>", 404


@app.route('/vendor/<path:filename>')
def vendor_files(filename):
    """提供本地打包的前端依赖（如 html2canvas），避免设备离线时依赖外网 CDN。"""
    vp = os.path.join(SCRIPT_DIR, 'vendor')
    fp = os.path.join(vp, filename)
    if os.path.isfile(fp):
        return send_file(fp)
    return "<h1>vendor 文件不存在</h1>", 404


@app.route('/api/status')
def api_status():
    return jsonify({'connected': True, 'type': 'local', 'host': '本机', 'device_info': get_device_info()})


@app.route('/api/device_info')
def api_device_info():
    return jsonify(get_device_info())


# ============= HUD 实时数据（openpilot 实时通道订阅）=============
# 通过 openpilot 的 cereal 实时通道(ipc:///tmp/cereal)订阅 carState/controlsState/deviceState。
# 取不到真实数据时优雅降级为演示模式(demo=True)，绝不卡死或崩溃。
import math  # 仅用于演示数据

HUD_SUB = None
HUD_IMPORT_ERR = None
OPENPILOT_DIR = '/data/openpilot'


def _init_hud_sub():
    """懒加载 SubMaster 订阅器，只初始化一次；失败则永久降级 demo。"""
    global HUD_SUB, HUD_IMPORT_ERR
    if HUD_SUB is not None or HUD_IMPORT_ERR is not None:
        return HUD_SUB
    try:
        if OPENPILOT_DIR not in sys.path:
            sys.path.insert(0, OPENPILOT_DIR)
        SubMaster = None
        # 兼容不同 openpilot 版本的 import 路径
        try:
            from openpilot.common.realtime import messaging
            SubMaster = messaging.SubMaster
        except Exception:
            try:
                from cereal.messaging import SubMaster
            except Exception as e:
                HUD_IMPORT_ERR = 'import failed: %s' % e
                return None
        HUD_SUB = SubMaster(['carState', 'controlsState', 'deviceState'])
        return HUD_SUB
    except Exception as e:
        HUD_IMPORT_ERR = str(e)
        return None


def _demo_hud():
    """演示模式：生成平滑波动的数据，让仪表盘始终有动效（非真实车况）。"""
    t = time.time()
    speed = max(0.0, (math.sin(t / 5.0) * 0.5 + 0.5) * 80 + 8 * math.sin(t / 1.3))
    steer = math.sin(t / 3.0) * 120
    accel = math.cos(t / 2.0) * 1.6
    g = max(0.0, math.sin(t / 4.0))
    return {
        'demo': True,
        'speed': round(speed, 1),
        'steer': round(steer, 1),
        'accel': round(accel, 2),
        'gas': round(g, 2),
        'brake': round(max(0.0, -math.sin(t / 4.0)), 2),
        'enabled': True, 'active': True, 'gear': 'D',
        'cpuTemp': round(45 + 4 * math.sin(t / 7.0), 1),
        'network': 1, 'netStrength': 3, 'freeSpace': 62,
    }


def get_hud_data():
    sm = _init_hud_sub()
    if sm is None:
        return _demo_hud()
    try:
        sm.update(0)  # 非阻塞，立即取当前最新帧
        cs = sm['carState']
        ctl = sm['controlsState']
        ds = sm['deviceState']
        v = cs.vEgo * 3.6
        steer = cs.steeringAngleDeg
        a = cs.aEgo
        gas = getattr(cs, 'gas', 0.0)
        brake = getattr(cs, 'brake', 0.0)
        enabled = bool(getattr(ctl, 'enabled', False))
        active = bool(getattr(ctl, 'active', False))
        gear = str(getattr(cs, 'gear', '') or 'N')
        cpu = getattr(ds, 'cpuTempC', [0]) or [0]
        cpu = max(cpu) if isinstance(cpu, (list, tuple)) else cpu
        net = getattr(ds, 'networkType', 0)
        net_s = getattr(ds, 'networkStrength', 0)
        free = getattr(ds, 'freeSpacePercent', 0)
        return {
            'demo': False,
            'speed': round(v, 1),
            'steer': round(steer, 1),
            'accel': round(a, 2),
            'gas': round(gas, 2),
            'brake': round(brake, 2),
            'enabled': enabled, 'active': active, 'gear': gear,
            'cpuTemp': round(cpu, 1),
            'network': int(net), 'netStrength': int(net_s),
            'freeSpace': round(free, 1),
        }
    except Exception as e:
        return {'demo': True, 'error': str(e), **_demo_hud()}


@app.route('/api/hud')
def api_hud():
    return jsonify(get_hud_data())


HIDDEN_PARAMS = [
    'SoundVolumeAdjust', 'SoundVolumeAdjustEngage',
    'DongleId', 'Version', 'GitBranch', 'GitCommit', 'CarName',
    'BootCount', 'PandaFirmwareVersion', 'PandaHardwareType', 'CameraType',
    'FirmwareVersion', 'PendingBranchFetch', 'LastUpdateCheck', 'UpdateAvailable',
    'HasRelief', 'HasSrm', 'HasAutoResumeFromStop', 'HasRadar',
    'HyundaiCameraSCC', 'CanfdHDA2', 'EnableRadarTracks', 'EnableCornerRadar', 'EnableEscc',
    'IsRHD', 'Passive',
]


def is_param_hidden(fname):
    for h in HIDDEN_PARAMS:
        if fname == h or fname.startswith(h):
            return True
    return False


@app.route('/api/params', methods=['GET'])
def api_get_params():
    params = {}
    sorted_keys = []
    try:
        for fname in sorted(os.listdir(PARAMS_DIR)):
            fpath = os.path.join(PARAMS_DIR, fname)
            if os.path.isfile(fpath) and fname != '.LOCK':
                if is_param_hidden(fname):
                    continue
                try:
                    with open(fpath, 'r') as f:
                        val = f.read()
                    if len(val.encode('utf-8')) <= 16:
                        params[fname] = val
                        sorted_keys.append(fname)
                except:
                    pass
    except Exception as e:
        return jsonify({'success': False, 'message': str(e), 'params': {}, 'count': 0})
    return jsonify({'success': True, 'message': f'成功获取 {len(params)} 个参数', 'params': params, 'sorted_keys': sorted_keys, 'count': len(params)})


@app.route('/api/params', methods=['POST'])
def api_set_params():
    data = request.json
    if not data:
        return jsonify({'success': False, 'message': '没有参数'})
    count = 0
    try:
        for key, value in data.items():
            sk = safe_key(key)
            if not sk:
                continue
            with open(os.path.join(PARAMS_DIR, sk), 'w') as f:
                f.write(str(value))
            count += 1
        return jsonify({'success': True, 'message': f'已保存 {count} 个参数'})
    except Exception as e:
        return jsonify({'success': False, 'message': f'保存失败: {str(e)}'})


@app.route('/api/param/delete', methods=['POST'])
def api_delete_param():
    data = request.json
    key = data.get('key', '')
    sk = safe_key(key)
    fpath = os.path.join(PARAMS_DIR, sk)
    if os.path.isfile(fpath):
        os.remove(fpath)
        return jsonify({'success': True, 'message': f'已删除: {key}'})
    return jsonify({'success': False, 'message': f'参数不存在: {key}'})


# ============= 分支识别与 dp/sp 自定义参数说明库 =============
def detect_branch():
    """识别当前运行的 openpilot 衍生版（原版 / dragonpilot / sunnypilot / frogpilot）。
    优先读 GitBranch 参数，再读 /data/openpilot/.git/HEAD 与 config；匹配不到回退 'openpilot'。"""
    def _match(s):
        s = (s or '').lower()
        if 'dragonpilot' in s:
            return 'dragonpilot'
        if 'sunny' in s:
            return 'sunnypilot'
        if 'frogpilot' in s:
            return 'frogpilot'
        if 'commaai' in s or 'openpilot' in s:
            return 'openpilot'
        return None
    try:
        gb = os.path.join(PARAMS_DIR, 'GitBranch')
        if os.path.isfile(gb):
            r = _match(open(gb, 'r').read().strip())
            if r:
                return r
        head = '/data/openpilot/.git/HEAD'
        if os.path.isfile(head):
            r = _match(open(head, 'r').read().strip())
            if r:
                return r
        cfg = '/data/openpilot/.git/config'
        if os.path.isfile(cfg):
            r = _match(open(cfg, 'r').read())
            if r:
                return r
    except Exception:
        pass
    return 'openpilot'


# 各分支常见自定义参数说明（中文）。框架可随时扩展；工具箱识别分支后把对应说明叠加到内置库。
# 仅覆盖“分支特有的自定义参数”，原版/carrot 参数已由前端内置 PN_DESC 覆盖。
BRANCH_META = {
    'dragonpilot': {
        'names': {
            'dp_long': 'DP 纵向控制', 'dp_toggle': 'DP 总开关', 'dp_following_profile': 'DP 跟车风格',
            'dp_accel_profile': 'DP 加减速风格', 'dp_steering_limit': 'DP 转向限速', 'dp_lat_lane_priority': 'DP 车道优先',
            'dp_allow_gas': 'DP 允许油门', 'dp_allow_engage_without_stock_dc': 'DP 无原厂巡航也可启用',
            'dp_camera_offset': 'DP 摄像头偏移', 'dp_path_offset': 'DP 路径偏移', 'dp_tire_km': 'DP 轮胎里程',
            'dp_ignore_can_valid': 'DP 忽略 CAN 校验', 'dp_engine_sound': 'DP 引擎音效', 'dp_dots': 'DP 转向点显示',
            'dp_device_shutdown': 'DP 定时关机', 'dp_logger': 'DP 扩展日志', 'dp_upload_raw': 'DP 上传原始数据',
            'dp_atl': 'DP 自动扭矩限速', 'dp_print': 'DP 调试打印', 'dp_gas_to_100': 'DP 油门至100%',
            'dp_gear_check': 'DP 档位检测', 'dp_allow_alps': 'DP 允许 ALPS', 'dp_enable_joystick': 'DP 启用摇杆',
            'dp_sound_rate': 'DP 提示音频率', 'dp_using_daemon': 'DP 守护进程', 'dp_panda_sync': 'DP Panda 同步',
            'dp_audible_alert_mode': 'DP 提示音模式', 'dp_steering_on_signal': 'DP 转向时微调', 'dp_animate_steering': 'DP 转向动画',
            'dp_camera_offset_video': 'DP 视频摄像头偏移', 'dp_calibration': 'DP 标定方式', 'dp_experimental_long': 'DP 实验性纵向',
            'dp_allow_brake': 'DP 允许刹车', 'dp_gear_confirm': 'DP 换挡确认',
        },
        'desc': {
            'dp_long': '启用 dragonpilot 自定义纵向控制（替代原厂）',
            'dp_toggle': 'DP 功能总开关',
            'dp_following_profile': '跟车距离/风格档位（0=最近，越大越远）',
            'dp_accel_profile': '加减速激进度档位',
            'dp_steering_limit': '方向盘转角限速（度/秒）',
            'dp_lat_lane_priority': '横向控制是否优先跟随车道线',
            'dp_allow_gas': '允许 DP 控制油门加速',
            'dp_allow_engage_without_stock_dc': '未开启原厂定速时也允许启用 openpilot',
            'dp_camera_offset': '摄像头相对车道中心的横向偏移修正',
            'dp_path_offset': '规划路径横向偏移修正',
            'dp_tire_km': '轮胎累计里程（胎压/磨损提示用）',
            'dp_ignore_can_valid': '忽略部分 CAN 信号有效性校验（老旧车型兼容）',
            'dp_engine_sound': '播放引擎模拟音效',
            'dp_dots': 'HUD 显示转向/路径圆点',
            'dp_device_shutdown': '离车后自动关机延时（分钟，0=不关）',
            'dp_logger': '启用 DP 扩展日志',
            'dp_upload_raw': '上传原始传感器数据到云端',
            'dp_atl': '根据路况自动限制扭矩（Auto Torque Limit）',
            'dp_print': '终端打印调试信息',
            'dp_gas_to_100': '油门请求可直接到 100%',
            'dp_gear_check': '启用档位（PRND）检测',
            'dp_allow_alps': '允许 ALPS 功能',
            'dp_enable_joystick': '启用方向盘摇杆（调试用）',
            'dp_sound_rate': '提示音播放速率',
            'dp_using_daemon': 'DP 以守护进程方式运行',
            'dp_panda_sync': '与 Panda 同步参数',
            'dp_audible_alert_mode': '提示音模式选择',
            'dp_steering_on_signal': '打转向灯时微调转向',
            'dp_animate_steering': '方向盘动画显示',
            'dp_camera_offset_video': '视频流摄像头偏移',
            'dp_calibration': '摄像头标定方式',
            'dp_experimental_long': '启用实验性纵向算法',
            'dp_allow_brake': '允许 DP 主动刹车',
            'dp_gear_confirm': '换挡需确认',
        },
        'bool_params': [
            'dp_long', 'dp_toggle', 'dp_lat_lane_priority', 'dp_allow_gas',
            'dp_allow_engage_without_stock_dc', 'dp_ignore_can_valid', 'dp_engine_sound', 'dp_dots',
            'dp_logger', 'dp_upload_raw', 'dp_atl', 'dp_print', 'dp_gas_to_100', 'dp_gear_check',
            'dp_allow_alps', 'dp_enable_joystick', 'dp_using_daemon', 'dp_panda_sync',
            'dp_steering_on_signal', 'dp_animate_steering', 'dp_experimental_long', 'dp_allow_brake',
            'dp_gear_confirm',
        ],
    },
    'sunnypilot': {
        'names': {
            'sp_experimental_mode': 'SP 实验模式', 'sp_mads_enabled': 'SP MADS 启用', 'sp_personality_profile': 'SP 驾驶性格',
            'sp_auto_resume': 'SP 自动恢复', 'sp_speed_limit_control': 'SP 限速控制', 'sp_speed_limit_delta': 'SP 限速偏移',
            'sp_speed_limit_factor': 'SP 限速系数', 'sp_navigation_based_speed_adjust': 'SP 导航限速调整',
            'sp_traffic_light_enabled': 'SP 红绿灯启用', 'sp_stop_at_stop_sign': 'SP 停止标志停车',
            'sp_turn_signal_confirmation': 'SP 转向灯确认', 'sp_lane_change_time': 'SP 变道时间',
            'sp_obd': 'SP OBD 启用', 'sp_obd_port': 'SP OBD 端口', 'sp_auto_enabled': 'SP 自动启用',
            'sp_lkas_button': 'SP LKAS 按钮', 'sp_disable_offroad_alert': 'SP 关闭离车提醒',
            'sp_lateral_control': 'SP 横向控制', 'sp_longitudinal_control': 'SP 纵向控制',
            'sp_steer_ratio': 'SP 转向比', 'sp_torque_factor': 'SP 扭矩系数', 'sp_lat_lane_priority': 'SP 车道优先',
            'sp_cruise_state': 'SP 巡航状态', 'sp_cruise_btn': 'SP 巡航按钮', 'sp_ui_mode': 'SP 界面模式',
            'sp_hso': 'SP 高速优化', 'sp_road_speed_adjust': 'SP 道路限速调整', 'sp_auto_navi_speed': 'SP 自动导航限速',
        },
        'desc': {
            'sp_experimental_mode': '启用 sunnypilot 实验性（更激进）驾驶模式',
            'sp_mads_enabled': '启用 MADS（手动领航）模式',
            'sp_personality_profile': '驾驶性格档位（0=温和 1=标准 2=激进）',
            'sp_auto_resume': '红灯/停车后自动恢复巡航',
            'sp_speed_limit_control': '根据地图/识别限速自动控制车速',
            'sp_speed_limit_delta': '限速上下偏移量（km/h，正=上限 负=下限）',
            'sp_speed_limit_factor': '限速系数（乘性）',
            'sp_navigation_based_speed_adjust': '基于导航路线自动调整限速',
            'sp_traffic_light_enabled': '启用红绿灯识别与启停',
            'sp_stop_at_stop_sign': '在停止标志处停车',
            'sp_turn_signal_confirmation': '变道需打转向灯确认',
            'sp_lane_change_time': '自动变道最小时间间隔（秒）',
            'sp_obd': '通过 OBD 读取车速/转速',
            'sp_obd_port': 'OBD 端口号',
            'sp_auto_enabled': '启动即自动启用',
            'sp_lkas_button': 'LKAS 按钮行为映射',
            'sp_disable_offroad_alert': '关闭离车安全提醒',
            'sp_lateral_control': '横向控制方式（扭矩/角度）',
            'sp_longitudinal_control': '纵向控制开关（0=原厂 1=SP）',
            'sp_steer_ratio': '转向比修正系数',
            'sp_torque_factor': '扭矩输出系数',
            'sp_lat_lane_priority': '横向优先跟随车道线',
            'sp_cruise_state': '巡航状态显示模式',
            'sp_cruise_btn': '巡航按钮功能映射',
            'sp_ui_mode': 'HUD 界面布局模式',
            'sp_hso': '高速优化开关',
            'sp_road_speed_adjust': '道路限速整体调整',
            'sp_auto_navi_speed': '根据导航自动限速',
        },
        'bool_params': [
            'sp_experimental_mode', 'sp_mads_enabled', 'sp_auto_resume', 'sp_traffic_light_enabled',
            'sp_stop_at_stop_sign', 'sp_turn_signal_confirmation', 'sp_obd', 'sp_auto_enabled',
            'sp_lkas_button', 'sp_disable_offroad_alert', 'sp_lat_lane_priority', 'sp_hso',
        ],
    },
    'frogpilot': {
        'names': {
            'FrogPilot': 'FrogPilot 母开关', 'FrogTrafficLight': '红绿灯识别', 'FrogStandState': '待机模式',
            'FrogSNG': 'SNG 启停补全（无原厂SNG车型）', 'FrogTheme': 'Frog 主题', 'FrogModel': '驾驶模型选择',
            'FrogAlertVolume': '提示音量', 'FrogRandomEvents': '随机事件音效', 'FrogSounds': 'Frog 音效',
            'FrogConditionalExperimental': '条件实验模式', 'FrogInitialExperimentalState': '初始实验模式状态',
            'FrogAccelerationProfile': '加速风格', 'FrogSpeedLimitController': '限速控制', 'FrogTrafficMode': '交通模式',
            'FrogSportMode': '运动模式', 'FrogEcoMode': '节能模式', 'FrogIncreasedStoppedDistance': '增大停止距离',
            'FrogLaneWidth': '车道线宽度', 'FrogPathWidth': '路径宽度', 'FrogEdgeWidth': '路沿宽度',
            'FrogRoadUI': '道路UI', 'FrogCompass': '指南针', 'FrogCameraView': '摄像头视图',
            'FrogGreenLightAlert': '绿灯提醒', 'FrogTurnLaneChange': '无感变道', 'FrogSteeringOnSignal': '转向灯时微调',
            'FrogNNFF': 'NNFF 平滑转向', 'FrogZSS': 'ZSS 转向支持', 'FrogAlwaysOnLateral': '常驻横向控制',
            'FrogLateralTorque': '横向扭矩控制', 'FrogReverseCruise': '倒车巡航', 'FrogStockLateral': '原厂横向',
            'FrogGMMode': 'GM 模式', 'FrogAutoShutdown': '自动关机', 'FrogScreenBrightness': '屏幕亮度',
            'FrogVisionTurnSpeedController': '视觉弯道限速', 'FrogMapTurnSpeedController': '地图弯道限速',
            'FrogAdjacentPathLines': '相邻车道线', 'FrogBlindSpotPath': '盲区路径', 'FrogSilentMode': '静音模式',
            'FrogCustomTheme': '自定义主题', 'FrogCumulativeDistance': '累计里程', 'FrogDeviceShutdown': '离车自动关机',
        },
        'desc': {
            'FrogPilot': 'FrogPilot 总开关（关闭则回落原厂体验）',
            'FrogTrafficLight': '识别红绿灯并据此启停',
            'FrogStandState': '待机模式：熄屏但保持后台，重要提醒时唤醒',
            'FrogSNG': '为无原厂 Stop&Go 的车型补全启停功能',
            'FrogTheme': '启用 Frog 青蛙主题（含配色与图标）',
            'FrogModel': '选择使用的驾驶模型（不同版本/风格）',
            'FrogAlertVolume': '各类提示音音量',
            'FrogRandomEvents': '行驶中随机触发 Frog 彩蛋音效',
            'FrogSounds': '启用 Frog 专属音效',
            'FrogConditionalExperimental': '满足条件时自动激活实验模式（路口/弯道/红灯等）',
            'FrogInitialExperimentalState': '上车时实验模式的初始状态',
            'FrogAccelerationProfile': '加速激进度档位',
            'FrogSpeedLimitController': '按地图/识别限速自动控制车速',
            'FrogTrafficMode': '针对拥堵路况的驾驶模式',
            'FrogSportMode': '运动加速/减速曲线',
            'FrogEcoMode': '节能加速/减速曲线',
            'FrogIncreasedStoppedDistance': '前车静止时增大本车停车距离',
            'FrogLaneWidth': 'HUD 车道线显示宽度',
            'FrogPathWidth': 'HUD 规划路径显示宽度',
            'FrogEdgeWidth': 'HUD 路沿显示宽度',
            'FrogRoadUI': '道路 UI 自定义显示',
            'FrogCompass': '屏幕显示指南针',
            'FrogCameraView': '选择偏好摄像头视图',
            'FrogGreenLightAlert': '绿灯亮起时提醒',
            'FrogTurnLaneChange': '无方向盘拨杆提示的变道',
            'FrogSteeringOnSignal': '打转向灯时暂停横向控制/微调',
            'FrogNNFF': '启用 NNFF 神经网络前馈使转向更平滑',
            'FrogZSS': '启用 ZSS 转向传感器支持',
            'FrogAlwaysOnLateral': '仅按巡航键即激活横向控制（常驻）',
            'FrogLateralTorque': '使用扭矩式横向控制',
            'FrogReverseCruise': '倒车时保持巡航',
            'FrogStockLateral': '横向控制回落原厂算法',
            'FrogGMMode': 'GM 车型专属模式',
            'FrogAutoShutdown': '离车后自动关机延时',
            'FrogScreenBrightness': '屏幕亮度（onroad/offroad 分别）',
            'FrogVisionTurnSpeedController': '基于视觉的弯道自动减速',
            'FrogMapTurnSpeedController': '基于地图的弯道自动减速',
            'FrogAdjacentPathLines': '显示相邻车道测量线',
            'FrogBlindSpotPath': '显示盲区路径指示',
            'FrogSilentMode': '完全静音驾驶',
            'FrogCustomTheme': '自定义 UI 主题',
            'FrogCumulativeDistance': '显示累计驾驶里程',
            'FrogDeviceShutdown': '离车自动关机',
        },
        'bool_params': [
            'FrogPilot', 'FrogTrafficLight', 'FrogStandState', 'FrogSNG', 'FrogTheme', 'FrogRandomEvents',
            'FrogSounds', 'FrogConditionalExperimental', 'FrogInitialExperimentalState', 'FrogTrafficMode',
            'FrogSportMode', 'FrogEcoMode', 'FrogIncreasedStoppedDistance', 'FrogRoadUI', 'FrogCompass',
            'FrogGreenLightAlert', 'FrogTurnLaneChange', 'FrogSteeringOnSignal', 'FrogNNFF', 'FrogZSS',
            'FrogAlwaysOnLateral', 'FrogLateralTorque', 'FrogReverseCruise', 'FrogStockLateral', 'FrogGMMode',
            'FrogAutoShutdown', 'FrogBlindSpotPath', 'FrogSilentMode', 'FrogCustomTheme', 'FrogDeviceShutdown',
        ],
    },
}


@app.route('/api/param_meta')
def api_param_meta():
    """返回当前分支识别结果与对应自定义参数说明库，前端合并到内置说明。"""
    branch = detect_branch()
    meta = BRANCH_META.get(branch, {})
    return jsonify({
        'branch': branch,
        'names': meta.get('names', {}),
        'desc': meta.get('desc', {}),
        'bool_params': meta.get('bool_params', []),
    })


def ensure_tmux_log():
    """设备端创建一个 tmux 会话 c3logs 持续 tail 工具箱日志，方便在设备 shell 执行 `tmux a -t c3logs` 实时查看。"""
    try:
        log_path = os.path.join(SCRIPT_DIR, 'server.log')
        cmd = "tmux has-session -t c3logs 2>/dev/null || tmux new-session -d -s c3logs 'tail -F %s'" % log_path
        subprocess.run(cmd, shell=True, timeout=5)
    except Exception:
        pass


@app.route('/api/command', methods=['POST'])
def api_command():
    data = request.json
    cmd = data.get('command', '').strip()
    if not cmd:
        return jsonify({'success': False, 'message': '命令不能为空'})
    blocked = ['rm -rf /', 'rm -rf /*', 'mkfs', 'dd if=', '> /dev/sd', 'chmod -R 777 /']
    for b in blocked:
        if b in cmd:
            return jsonify({'success': False, 'message': f'安全限制: 禁止执行此命令'})
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        return jsonify({'success': True, 'result': {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}})
    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'result': '命令执行超时(60秒)'})
    except Exception as e:
        return jsonify({'success': False, 'result': str(e)})


@app.route('/api/logstream')
def api_logstream():
    """实时日志流 (SSE)：网页终端执行 `tmux a` 时改用此接口持续推送 server.log 新内容。"""
    log_path = os.path.join(SCRIPT_DIR, 'server.log')

    def gen():
        yield "data: ┌── 实时日志 (tmux a) 已连接 ──┐\n\n"
        try:
            with open(log_path, 'rb') as f:
                # 先回放最近 30 行
                data = f.read()
                lines = data.split(b'\n')
                tail = lines[-31:-1] if len(lines) > 31 else lines[:-1]
                for ln in tail:
                    yield "data: " + ln.decode('utf-8', 'replace') + "\n\n"
                f.seek(0, 2)  # 定位到末尾，之后只推新内容
                while True:
                    chunk = f.read()
                    if chunk:
                        for ln in chunk.split(b'\n'):
                            if ln:
                                yield "data: " + ln.decode('utf-8', 'replace') + "\n\n"
                    else:
                        time.sleep(0.25)
        except GeneratorExit:
            pass
        except Exception as e:
            yield "data: [日志读取错误: %s]\n\n" % str(e)

    return Response(gen(), mimetype='text/event-stream',
                   headers={'Cache-Control': 'no-cache, no-transform',
                            'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@app.route('/api/backup', methods=['POST'])
def api_backup():
    params = {}
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except Exception as e:
        return jsonify({'success': False, 'message': f'创建备份目录失败: {e}'})
    try:
        for fname in os.listdir(PARAMS_DIR):
            fpath = os.path.join(PARAMS_DIR, fname)
            if os.path.isfile(fpath) and fname != '.LOCK':
                try:
                    with open(fpath, 'r') as f:
                        val = f.read()
                    if len(val.encode('utf-8')) <= 16:
                        params[fname] = val
                except:
                    pass
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取参数失败: {e}'})
    if params:
        ts = time.strftime('%Y%m%d_%H%M%S')
        fn = os.path.join(BACKUP_DIR, f'c3_params_{ts}.json')
        try:
            with open(fn, 'w', encoding='utf-8') as f:
                json.dump(params, f, indent=2, ensure_ascii=False)
            return jsonify({'success': True, 'message': f'备份成功', 'count': len(params), 'filename': os.path.basename(fn)})
        except Exception as e:
            return jsonify({'success': False, 'message': f'写入备份文件失败: {e}'})
    return jsonify({'success': False, 'message': '没有参数可备份'})


@app.route('/api/restore', methods=['POST'])
def api_restore():
    data = request.json
    params = data.get('params', {})
    if not params:
        return jsonify({'success': False, 'message': '没有参数数据'})
    count = 0
    try:
        for key, value in params.items():
            sk = safe_key(key)
            if not sk:
                continue
            with open(os.path.join(PARAMS_DIR, sk), 'w') as f:
                f.write(str(value))
            count += 1
        return jsonify({'success': True, 'message': f'已恢复 {count} 个参数'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/restart_op', methods=['POST'])
def api_restart_op():
    try:
        subprocess.run("pkill -f manager.py", shell=True, timeout=5)
        return jsonify({'success': True, 'message': 'openpilot 重启指令已发送'})
    except:
        return jsonify({'success': True, 'message': '重启指令已发送'})


@app.route('/api/reboot', methods=['POST'])
def api_reboot():
    try:
        subprocess.Popen(["sudo", "reboot"])
        return jsonify({'success': True, 'message': '设备重启命令已发送'})
    except:
        return jsonify({'success': True, 'message': '重启命令已发送'})


def do_auto_backup():
    """自动备份所有参数（完整备份，含隐藏参数）"""
    try:
        os.makedirs(AUTO_BACKUP_DIR, exist_ok=True)
    except Exception as e:
        raise RuntimeError(f"创建自动备份目录失败: {e}")
    params = {}
    try:
        for fname in os.listdir(PARAMS_DIR):
            fpath = os.path.join(PARAMS_DIR, fname)
            if os.path.isfile(fpath) and fname != '.LOCK':
                try:
                    with open(fpath, 'r') as f:
                        val = f.read()
                    if len(val.encode('utf-8')) <= 16:
                        params[fname] = val
                except:
                    pass
    except Exception as e:
        raise RuntimeError(f"读取参数目录失败: {e}")
    if not params:
        raise RuntimeError("没有可备份的参数")
    try:
        with open(AUTO_BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(params, f, indent=2, ensure_ascii=False)
    except Exception as e:
        raise RuntimeError(f"写入备份文件失败: {e}")
    return params


@app.route('/api/auto_backup')
def api_auto_backup():
    """检查并执行自动备份，返回状态"""
    try:
        need_backup = not os.path.isfile(AUTO_BACKUP_FILE)
        info = {'exists': not need_backup, 'path': AUTO_BACKUP_FILE}
        if need_backup:
            params = do_auto_backup()
            info['created'] = True
            info['count'] = len(params)
            info['message'] = f'已自动备份 {len(params)} 个参数'
        else:
            try:
                with open(AUTO_BACKUP_FILE, 'r') as f:
                    params = json.load(f)
                info['count'] = len(params)
                info['message'] = f'自动备份已存在，共 {len(params)} 个参数'
            except Exception:
                # 文件损坏，重新备份
                params = do_auto_backup()
                info['created'] = True
                info['count'] = len(params)
                info['message'] = f'自动备份文件已重建，共 {len(params)} 个参数'
        return jsonify(info)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'path': AUTO_BACKUP_FILE, 'message': f'自动备份失败: {e}'})


@app.route('/api/backups')
def api_list_backups():
    """列出所有备份文件（手动+自动）"""
    # 双保险：即使启动时建目录失败，这里也确保目录存在，避免扫描被吞异常
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.makedirs(AUTO_BACKUP_DIR, exist_ok=True)
    except Exception:
        pass
    backups = []
    try:
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.endswith('.json'):
                fp = os.path.join(BACKUP_DIR, f)
                size = os.path.getsize(fp)
                mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(fp)))
                backups.append({'name': f, 'size': size, 'time': mtime, 'type': 'manual'})
    except:
        pass
    # 添加自动备份文件
    if os.path.isfile(AUTO_BACKUP_FILE):
        size = os.path.getsize(AUTO_BACKUP_FILE)
        mtime = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(AUTO_BACKUP_FILE)))
        backups.append({'name': 'auto_full_params.json', 'size': size, 'time': mtime, 'type': 'auto', 'path': AUTO_BACKUP_FILE})
    return jsonify({'backups': backups})


@app.route('/api/backup/download/<filename>')
def api_download_backup(filename):
    """下载备份文件（支持手动和自动备份目录）"""
    sk = safe_key(filename)
    if not sk.endswith('.json'):
        sk += '.json'
    # 先查手动备份目录，再查自动备份目录
    fp = os.path.join(BACKUP_DIR, sk)
    if not os.path.isfile(fp):
        fp = os.path.join(AUTO_BACKUP_DIR, sk)
    if os.path.isfile(fp):
        return send_from_directory(os.path.dirname(fp), os.path.basename(fp), as_attachment=True)
    return jsonify({'success': False, 'message': '文件不存在'})


# ============= 完整备份/恢复（/data/openpilot 整目录）=============

@app.route('/api/op_backup', methods=['POST'])
def api_op_backup():
    """后台打包 /data/openpilot 为 tar.gz 备份包（命名含时间戳）"""
    ts = time.strftime('%Y-%m-%d_%H-%M-%S')
    fname = OP_BK_PREFIX + ts + '.tar.gz'
    out_path = os.path.join(OP_BK_DIR, fname)
    task_id = str(int(time.time() * 1000))
    OP_TASKS[task_id] = {'status': 'running', 'message': '正在打包 /data/openpilot ...', 'done': False}
    def run():
        try:
            import tarfile
            # 先统计总大小与文件清单，用于实时进度
            file_list = []
            total = 0
            for root, dirs, files in os.walk('/data/openpilot'):
                for fn in files:
                    p = os.path.join(root, fn)
                    file_list.append(p)
                    try:
                        total += os.path.getsize(p)
                    except Exception:
                        pass
            done = 0
            OP_TASKS[task_id] = {'status': 'running', 'message': '正在打包 /data/openpilot ...', 'done': False, 'progress': 0}
            with tarfile.open(out_path, 'w:gz') as tar:
                for p in file_list:
                    try:
                        tar.add(p, arcname=p)
                    except Exception:
                        pass
                    try:
                        done += os.path.getsize(p)
                    except Exception:
                        pass
                    if total > 0:
                        OP_TASKS[task_id]['progress'] = min(99, int(done * 100 / total))
            if os.path.isfile(out_path):
                sz = os.path.getsize(out_path)
                OP_TASKS[task_id] = {
                    'status': 'done', 'done': True, 'progress': 100,
                    'message': f'备份完成: {fname} ({sz/1024/1024:.0f} MB)',
                    'filename': fname, 'size': sz,
                }
            else:
                OP_TASKS[task_id] = {'status': 'error', 'done': True, 'progress': 100, 'message': '备份失败：未生成文件'}
        except Exception as e:
            OP_TASKS[task_id] = {'status': 'error', 'done': True, 'progress': 100, 'message': f'备份异常: {e}'}
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/op_task/<task_id>')
def api_op_task(task_id):
    """查询后台任务（备份/恢复）状态"""
    t = OP_TASKS.get(task_id)
    if not t:
        return jsonify({'done': True, 'status': 'error', 'message': '任务不存在或已过期'})
    return jsonify(t)


@app.route('/api/op_backups')
def api_op_backups():
    """列出 /data 下所有完整备份包"""
    items = []
    try:
        for f in os.listdir(OP_BK_DIR):
            if f.startswith(OP_BK_PREFIX) and f.endswith('.tar.gz'):
                fp = os.path.join(OP_BK_DIR, f)
                if os.path.isfile(fp):
                    sz = os.path.getsize(fp)
                    mt = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(fp)))
                    items.append({'name': f, 'size': sz, 'time': mt})
    except Exception:
        pass
    items.sort(key=lambda x: x['time'], reverse=True)
    return jsonify({'backups': items})


@app.route('/api/op_backup/download/<path:filename>')
def api_op_backup_download(filename):
    """下载完整备份包（仅允许前缀匹配的备份包，防路径穿越）"""
    if not (filename.startswith(OP_BK_PREFIX) and filename.endswith('.tar.gz')):
        return jsonify({'success': False, 'message': '非法文件名'}), 400
    fp = os.path.join(OP_BK_DIR, filename)
    if os.path.isfile(fp):
        return send_from_directory(OP_BK_DIR, filename, as_attachment=True)
    return jsonify({'success': False, 'message': '文件不存在'}), 404


def _restore_package(pkg_path, reboot_after, task_id):
    """实际的恢复逻辑：停 openpilot → 解包到 / → 可选重启。在后台线程执行。"""
    try:
        import tarfile
        # 先停 openpilot，避免运行中文件被覆盖导致不一致（不影响工具箱本身）
        subprocess.run("pkill -f manager.py", shell=True, timeout=5)
        time.sleep(2)
        # 统计总大小
        total = 0
        members = []
        with tarfile.open(pkg_path, 'r:*') as tar:
            try:
                tar.extraction_filter = (lambda m, path: m)
            except Exception:
                pass
            members = tar.getmembers()
            for m in members:
                total += m.size
        done = 0
        OP_TASKS[task_id] = {'status': 'running', 'message': '正在解压恢复 /data/openpilot ...', 'done': False, 'progress': 0}
        with tarfile.open(pkg_path, 'r:*') as tar:
            try:
                tar.extraction_filter = (lambda m, path: m)
            except Exception:
                pass
            for m in members:
                try:
                    tar.extract(m, '/')
                except Exception:
                    pass
                done += m.size
                if total > 0:
                    OP_TASKS[task_id]['progress'] = min(99, int(done * 100 / total))
        try:
            os.remove(pkg_path)  # 上传的临时包用完后清理
        except Exception:
            pass
        msg = '恢复完成: ' + os.path.basename(pkg_path)
        if reboot_after:
            msg += '，设备即将重启...'
            OP_TASKS[task_id] = {'status': 'done', 'done': True, 'progress': 100, 'message': msg}
            time.sleep(1)
            subprocess.Popen(["sudo", "reboot"])
        else:
            OP_TASKS[task_id] = {
                'status': 'done', 'done': True, 'progress': 100,
                'message': msg + '（建议手动重启设备使 openpilot 重新加载）',
            }
    except subprocess.TimeoutExpired:
        OP_TASKS[task_id] = {'status': 'error', 'done': True, 'progress': 100, 'message': '恢复超时（>30 分钟）'}
    except Exception as e:
        OP_TASKS[task_id] = {'status': 'error', 'done': True, 'progress': 100, 'message': f'恢复异常: {e}'}


@app.route('/api/op_restore', methods=['POST'])
def api_op_restore():
    """从设备内已有备份包恢复"""
    data = request.json or {}
    fname = data.get('filename', '')
    if not (isinstance(fname, str) and fname.startswith(OP_BK_PREFIX) and fname.endswith('.tar.gz')):
        return jsonify({'success': False, 'message': '非法的备份包名称'})
    pkg = os.path.join(OP_BK_DIR, fname)
    if not os.path.isfile(pkg):
        return jsonify({'success': False, 'message': '备份包不存在'})
    reboot_after = bool(data.get('reboot', False))
    task_id = str(int(time.time() * 1000))
    OP_TASKS[task_id] = {'status': 'running', 'message': f'正在恢复: {fname}', 'done': False}
    threading.Thread(target=_restore_package, args=(pkg, reboot_after, task_id), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


@app.route('/api/op_restore_upload', methods=['POST'])
def api_op_restore_upload():
    """上传本地备份包并恢复"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未收到文件'})
    f = request.files['file']
    if not (f.filename.endswith('.tar.gz') or f.filename.endswith('.tgz')):
        return jsonify({'success': False, 'message': '仅支持 .tar.gz / .tgz 备份包'})
    tmp = os.path.join(OP_BK_DIR, OP_BK_PREFIX + 'upload_' + str(int(time.time() * 1000)) + '.tar.gz')
    try:
        f.save(tmp)
    except Exception as e:
        return jsonify({'success': False, 'message': f'保存上传文件失败: {e}'})
    reboot_after = request.form.get('reboot', 'false') in ('1', 'true', 'True')
    task_id = str(int(time.time() * 1000))
    OP_TASKS[task_id] = {'status': 'running', 'message': '正在从上传的备份包恢复...', 'done': False}
    threading.Thread(target=_restore_package, args=(tmp, reboot_after, task_id), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})


# ============= 开机屏定制（第一屏 splash / 第二屏背景图）=============

BG_PATH = '/usr/comma/bg.jpg'
SPLASH_BACKUP = '/data/splash_backup.bin'

def _find_splash():
    """探测 splash 分区设备路径（不同设备名可能不同，优先教程固定路径）"""
    cand = '/dev/block/bootdevice/by-name/splash'
    if os.path.exists(cand):
        return cand
    try:
        out = subprocess.run(['ls', '/dev/block/bootdevice/by-name/'],
                             capture_output=True, text=True, timeout=5)
        for line in out.stdout.split():
            if 'splash' in line:
                return '/dev/block/bootdevice/by-name/' + line
    except Exception:
        pass
    return None


@app.route('/api/bg_info', methods=['GET'])
def api_bg_info():
    """查询当前 Weston 背景图信息（第二屏）"""
    try:
        if os.path.isfile(BG_PATH):
            st = os.stat(BG_PATH)
            return jsonify({'exists': True, 'size': st.st_size,
                            'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))})
        return jsonify({'exists': False})
    except Exception as e:
        return jsonify({'exists': False, 'error': str(e)})


@app.route('/api/bg_backup', methods=['GET'])
def api_bg_backup():
    """下载当前背景图到电脑留存"""
    if os.path.isfile(BG_PATH):
        return send_from_directory('/usr/comma', 'bg.jpg', as_attachment=True)
    return jsonify({'success': False, 'message': '当前没有 bg.jpg'}), 404


@app.route('/api/bg_set', methods=['POST'])
def api_bg_set():
    """上传自定义背景图（jpg/png），remount rw 后写入 /usr/comma/bg.jpg 再 ro"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未收到图片'})
    f = request.files['file']
    if not (f.filename.endswith('.jpg') or f.filename.endswith('.jpeg') or f.filename.endswith('.png')):
        return jsonify({'success': False, 'message': '仅支持 .jpg / .jpeg / .png'})
    tmp = '/tmp/bg_upload_' + str(int(time.time() * 1000)) + os.path.splitext(f.filename)[1]
    try:
        f.save(tmp)
    except Exception as e:
        return jsonify({'success': False, 'message': f'保存失败: {e}'})
    reboot_after = request.form.get('reboot', 'false') in ('1', 'true', 'True')
    try:
        if os.path.isfile(BG_PATH):
            subprocess.run(['sudo', 'cp', BG_PATH, BG_PATH + '.bak'], timeout=10, check=False)
        subprocess.run(['sudo', 'mount', '-o', 'remount,rw', '/'], timeout=15, check=False)
        subprocess.run(['sudo', 'cp', tmp, BG_PATH], timeout=15, check=False)
        subprocess.run(['sudo', 'mount', '-o', 'remount,ro', '/'], timeout=15, check=False)
        try:
            os.remove(tmp)
        except Exception:
            pass
        msg = '背景图已替换，重启后生效'
        if reboot_after:
            msg += '，设备即将重启...'
            threading.Thread(target=lambda: (time.sleep(1), subprocess.Popen(['sudo', 'reboot'])), daemon=True).start()
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'替换失败: {e}'})


@app.route('/api/splash_info', methods=['GET'])
def api_splash_info():
    """查询 splash 分区信息（第一屏）"""
    sp = _find_splash()
    if not sp:
        return jsonify({'exists': False, 'error': '未找到 splash 分区'})
    try:
        sz = os.path.getsize(sp)
    except Exception:
        sz = 0
    return jsonify({'exists': True, 'path': sp, 'size': sz, 'has_backup': os.path.isfile(SPLASH_BACKUP)})


@app.route('/api/splash_backup', methods=['GET'])
def api_splash_backup():
    """下载已备份的 splash 分区 bin"""
    if os.path.isfile(SPLASH_BACKUP):
        return send_from_directory('/data', 'splash_backup.bin', as_attachment=True)
    return jsonify({'success': False, 'message': '尚未备份 splash 分区'}), 404


@app.route('/api/splash_set', methods=['POST'])
def api_splash_set():
    """上传 splash_new.bin，先备份原分区再 dd 写入 splash 分区（第一屏）"""
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': '未收到 bin 文件'})
    f = request.files['file']
    if not f.filename.endswith('.bin'):
        return jsonify({'success': False, 'message': '仅支持 .bin（splash_new.bin）'})
    sp = _find_splash()
    if not sp:
        return jsonify({'success': False, 'message': '未找到 splash 分区'})
    try:
        part_size = os.path.getsize(sp)
    except Exception:
        part_size = 0
    tmp = '/tmp/splash_upload_' + str(int(time.time() * 1000)) + '.bin'
    try:
        f.save(tmp)
    except Exception as e:
        return jsonify({'success': False, 'message': f'保存失败: {e}'})
    # 大小校验：bin 必须与原 splash 分区大小一致，避免写坏分区
    try:
        bin_size = os.path.getsize(tmp)
    except Exception:
        bin_size = 0
    if part_size and bin_size and bin_size != part_size:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return jsonify({'success': False, 'message': f'bin 大小({bin_size}) 与 splash 分区大小({part_size}) 不符，请用正确工具生成的 splash_new.bin'})
    reboot_after = request.form.get('reboot', 'false') in ('1', 'true', 'True')
    try:
        subprocess.run(['sudo', 'dd', 'if=' + sp, 'of=' + SPLASH_BACKUP, 'bs=1M'], timeout=120, check=False)
        subprocess.run(['sudo', 'dd', 'if=' + tmp, 'of=' + sp, 'bs=1M'], timeout=120, check=False)
        subprocess.run(['sudo', 'sync'], timeout=30, check=False)
        try:
            os.remove(tmp)
        except Exception:
            pass
        msg = '开机第一屏已刷入，重启后生效'
        if reboot_after:
            msg += '，设备即将重启...'
            threading.Thread(target=lambda: (time.sleep(1), subprocess.Popen(['sudo', 'reboot'])), daemon=True).start()
        return jsonify({'success': True, 'message': msg})
    except Exception as e:
        return jsonify({'success': False, 'message': f'刷入失败: {e}'})


if __name__ == '__main__':
    PORT = 5588
    # 确保目录存在
    for d in (BASE_DIR, BACKUP_DIR, AUTO_BACKUP_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception as e:
            print(f"[!] 无法创建目录 {d}: {e}")
    # 首次启动即自动备份，确保“初次连接”前备份已存在
    if not os.path.isfile(AUTO_BACKUP_FILE):
        try:
            do_auto_backup()
            print(f"[✓] 初始自动备份已创建: {AUTO_BACKUP_FILE}")
        except Exception as e:
            print(f"[!] 初始自动备份失败: {e}")
    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║     C3 设备工具箱 (本地模式)       ║")
    print("  ║     Carrotpilot cpv9-dev            ║")
    print("  ╠══════════════════════════════════════╣")
    print("  ║  访问地址: http://0.0.0.0:%d      ║" % PORT)
    print("  ║  参数目录: /data/params/d          ║")
    print("  ║  备份目录: /data/c3_toolbox/backups ║")
    print("  ║  自动备份: 已启用                  ║")
    print("  ╚══════════════════════════════════════╝")
    print(f"  自动备份文件: {AUTO_BACKUP_FILE}")
    print()
    ensure_tmux_log()
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)