#!/usr/bin/env python3
"""
C3 设备工具箱 - 设备本地版 v2 (Carrotpilot cpv9-dev)
=====================================================
直接运行在 C3 设备上，浏览器访问 http://设备IP:5588 即可
"""

import json, os, sys, time, subprocess, urllib.request, tarfile, io, threading, traceback, re, zipfile

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
VOICE_DIR = os.path.join(BASE_DIR, "voice")  # 用户上传的自定义语音存放目录

# 感知数据缓存（由后台采集线程填充，/api/perception 读取）
PERC_LOCK = threading.Lock()
PERC_DATA = {"available": False, "source": "none", "reason": "init", "boxes": [], "path": [], "lane_lines": []}

# 原车动作事件缓存（由后台采集线程填充，/api/car_events 读取）
EVENT_LOCK = threading.Lock()
EVENT_DATA = {
    "available": False, "reason": "init",
    "left_blinker": False, "right_blinker": False, "acc_enabled": False,
    "last_event": {"type": None, "seq": 0, "ts": 0.0},
}

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
VERSION = "1.2.0"
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




# ============= 路由 =============

@app.route('/')
def index():
    p = os.path.join(SCRIPT_DIR, 'c3_toolbox.html')
    if os.path.isfile(p):
        return send_file(p)
    return "<h1>c3_toolbox.html 不存在</h1>"


@app.route('/api/status')
def api_status():
    return jsonify({'connected': True, 'type': 'local', 'host': '本机', 'device_info': get_device_info()})


@app.route('/api/device_info')
def api_device_info():
    return jsonify(get_device_info())


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
        log_path = LOG_FILE
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
    log_path = LOG_FILE

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


# ============= SSE 进度推送 + 防卡心跳（整体备份 / 在线更新等长任务） =============

class SSEProgress:
    """统一的 SSE 事件构造器。前端用 fetch+ReadableStream 读取 data:{json}，按 t 字段分支。"""
    @staticmethod
    def log(m):
        return "data: " + json.dumps({"t": "log", "m": m}, ensure_ascii=False) + "\n\n"
    @staticmethod
    def progress(m, pct=None):
        return "data: " + json.dumps({"t": "progress", "m": m, "pct": pct}, ensure_ascii=False) + "\n\n"
    @staticmethod
    def done(m):
        return "data: " + json.dumps({"t": "done", "m": m}, ensure_ascii=False) + "\n\n"
    @staticmethod
    def error(m):
        return "data: " + json.dumps({"t": "error", "m": m}, ensure_ascii=False) + "\n\n"


def _fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return ("%.1f %s" % (n, unit)) if unit != "B" else ("%d %s" % (n, unit))
        n /= 1024
    return "%.1f PB" % n


def _sse_heartbeat(producer):
    """包装一个事件生产者 generator：队列驱动，超过 3 秒无事件则注入 ': hb' 心跳注释，
    既让前端知道连接存活（防中间网络/服务端静默掐断导致假死），又能在长任务中持续保活。"""
    import queue as _q
    q = _q.Queue()

    def _run():
        try:
            for ev in producer():
                q.put(("ev", ev))
        except GeneratorExit:
            pass
        except Exception as e:
            try:
                q.put(("ev", SSEProgress.error("%s: %s" % (type(e).__name__, e))))
            except Exception:
                pass
        finally:
            q.put(("stop", None))

    threading.Thread(target=_run, daemon=True).start()
    while True:
        try:
            kind, val = q.get(timeout=3)
        except _q.Empty:
            yield ": hb\n\n"
            continue
        if kind == "stop":
            break
        yield val


def _sse_headers():
    return {
        'Cache-Control': 'no-cache, no-transform',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
    }


@app.route('/api/update', methods=['POST'])
def api_update():
    """在线更新（SSE 流式进度 + 心跳防卡）：检测新版本→分块下载发布包(真实进度)→解压→延迟重启。"""
    def producer():
        sp = SSEProgress()
        yield sp.log("开始检查更新...")
        try:
            remote = fetch_remote_meta()
            rv = remote.get('version', '0')
            yield sp.log("远程版本 %s / 本地版本 %s" % (rv, VERSION))
            if cmp_version(rv, VERSION) <= 0:
                yield sp.done("已是最新版本 (%s)" % VERSION)
                return
            urls = resolve_tarball_urls(remote)
            data = io.BytesIO(); got = 0; total = 0; last_pct = [-1]; last_err = None
            for u in urls:
                if not u:
                    continue
                try:
                    req = urllib.request.Request(u, headers={'User-Agent': 'c3-toolbox-update'})
                    resp = urllib.request.urlopen(req, timeout=120)
                    total = int(resp.headers.get('Content-Length', '0') or 0)
                    yield sp.progress("下载发布包... 0%", 0)
                    while True:
                        c = resp.read(65536)
                        if not c:
                            break
                        data.write(c); got += len(c)
                        if total:
                            pct = int(got * 100 / total)
                            if pct != last_pct[0]:
                                last_pct[0] = pct
                                yield sp.progress("下载发布包... %d%%" % pct, pct)
                    break
                except Exception as e:
                    last_err = e
            if got == 0:
                if last_err:
                    raise last_err
                raise RuntimeError('无可用下载地址')
            yield sp.progress("下载完成，解压中...", 100)
            with tarfile.open(fileobj=io.BytesIO(data.getvalue()), mode='r:gz') as tf:
                # Python 3.12+ 要求显式指定 filter（PEP 706）；3.11 无该参数
                if sys.version_info >= (3, 12):
                    tf.extractall(BASE_DIR, filter='data')
                else:
                    tf.extractall(BASE_DIR)
            yield sp.log("解压完成，正在重启服务...")
            yield sp.done("更新完成，正在重启服务...")
            time.sleep(0.6)
            schedule_restart()
        except Exception as e:
            yield sp.error("更新失败 [%s]: %s" % (type(e).__name__, e))
    return Response(_sse_heartbeat(producer), mimetype='text/event-stream', headers=_sse_headers())


@app.route('/api/backup_full', methods=['POST'])
def api_backup_full():
    """整体备份（SSE 流式进度 + 心跳防卡）：在线打包 /data/openpilot + /data/params。
    铁律：绝不在开头 pkill openpilot——否则看门狗/热点重启设备会瞬断 SSH 会话 → 退出码 -1。
    tar 后台执行，每 2 秒汇报已打包大小；末尾 md5 + tar -tzf 解包比对文件数校验。"""
    def producer():
        sp = SSEProgress()
        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            yield sp.log("分析备份范围...")
            ts = time.strftime('%Y%m%d_%H%M%S')
            out = os.path.join(BACKUP_DIR, "c3_full_%s.tar.gz" % ts)
            srcs = [d for d in ("/data/openpilot", "/data/params") if os.path.isdir(d)]
            if not srcs:
                yield sp.error("未找到可备份目录（/data/openpilot、/data/params 均不存在）")
                return
            # 统计源文件数（含符号链接）作为校验分母
            src_count = 0
            try:
                outp = subprocess.run("find %s -type f -o -type l | wc -l" % " ".join(srcs),
                                      shell=True, capture_output=True, text=True, timeout=30)
                src_count = int((outp.stdout or "").strip() or 0)
            except Exception:
                src_count = 0
            yield sp.log("需备份 %d 个文件（openpilot 代码 + 参数）" % src_count)
            # 后台 tar（不带 pkill），轮询进度
            cmd = "tar -czf %s %s" % (out, " ".join(srcs))
            p = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            last_sz = [-1]
            while p.poll() is None:
                try:
                    sz = os.path.getsize(out)
                except Exception:
                    sz = 0
                if sz != last_sz[0]:
                    last_sz[0] = sz
                    yield sp.progress("打包中... %s" % _fmt_size(sz), None)
                time.sleep(2)
            if p.returncode != 0:
                yield sp.error("打包失败（tar 退出码 %d）" % p.returncode)
                return
            yield sp.progress("打包完成，校验中...", 100)
            md5 = ""
            try:
                r = subprocess.run("md5sum %s" % out, shell=True, capture_output=True, text=True, timeout=30)
                md5 = (r.stdout or "").split()[0]
            except Exception:
                pass
            arc_count = 0
            try:
                r = subprocess.run("tar -tzf %s | wc -l" % out, shell=True, capture_output=True, text=True, timeout=60)
                arc_count = int((r.stdout or "").strip() or 0)
            except Exception:
                arc_count = 0
            if src_count and arc_count and src_count == arc_count:
                yield sp.log("校验通过：备份含 %d 个文件，与源一致（md5 %s）" % (arc_count, md5[:8]))
            else:
                yield sp.log("⚠ 文件数不一致：源 %d / 包 %d（md5 %s），建议重试" % (src_count, arc_count, md5[:8]))
            yield sp.done("整体备份完成：%s（%d 文件，%s）" % (os.path.basename(out), arc_count, _fmt_size(os.path.getsize(out))))
        except Exception as e:
            yield sp.error("备份失败 [%s]: %s" % (type(e).__name__, e))
    return Response(_sse_heartbeat(producer), mimetype='text/event-stream', headers=_sse_headers())


@app.route('/api/backup_full/download/<filename>')
def api_download_backup_full(filename):
    """下载整体备份包（.tar.gz）"""
    sk = safe_key(filename)
    if not sk.endswith('.tar.gz'):
        return jsonify({'success': False, 'message': '仅支持 .tar.gz 备份文件'})
    fp = os.path.join(BACKUP_DIR, sk)
    if os.path.isfile(fp):
        return send_from_directory(BACKUP_DIR, sk, as_attachment=True)
    return jsonify({'success': False, 'message': '文件不存在'})


@app.route('/api/backup_full/list')
def api_list_backup_full():
    """列出整体备份包（.tar.gz）"""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except Exception:
        pass
    items = []
    try:
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.endswith('.tar.gz'):
                fp = os.path.join(BACKUP_DIR, f)
                items.append({'name': f, 'size': os.path.getsize(fp),
                              'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(fp)))})
    except Exception:
        pass
    return jsonify({'backups': items})


@app.route('/api/backup_full/delete/<filename>', methods=['POST'])
def api_delete_backup_full(filename):
    """删除整体备份包（.tar.gz），防路径穿越"""
    sk = safe_key(filename)
    if not sk.endswith('.tar.gz'):
        return jsonify({'success': False, 'message': '仅支持删除 .tar.gz 备份'})
    fp = os.path.join(BACKUP_DIR, sk)
    if os.path.isfile(fp):
        try:
            os.remove(fp)
            return jsonify({'success': True, 'message': '已删除 %s' % sk})
        except Exception as e:
            return jsonify({'success': False, 'message': '删除失败: %s' % e})
    return jsonify({'success': False, 'message': '备份文件不存在'})


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


@app.route('/api/restore/file', methods=['POST'])
def api_restore_file():
    """从设备内已有参数备份文件恢复（按文件名定位手动或自动备份目录）"""
    data = request.json or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'success': False, 'message': '未指定备份文件'})
    sk = safe_key(filename)
    if not sk.endswith('.json'):
        return jsonify({'success': False, 'message': '仅支持 .json 备份文件'})
    # 先查手动备份目录，再查自动备份目录
    fp = os.path.join(BACKUP_DIR, sk)
    if not os.path.isfile(fp):
        fp = os.path.join(AUTO_BACKUP_DIR, sk)
    if not os.path.isfile(fp):
        return jsonify({'success': False, 'message': '备份文件不存在'})
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            params = json.load(f)
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取备份文件失败: {e}'})
    if not isinstance(params, dict) or not params:
        return jsonify({'success': False, 'message': '备份文件内容无效（应为参数键值对）'})
    count = 0
    try:
        for key, value in params.items():
            skk = safe_key(key)
            if not skk:
                continue
            with open(os.path.join(PARAMS_DIR, skk), 'w') as f:
                f.write(str(value))
            count += 1
        return jsonify({'success': True, 'message': f'已从 {sk} 恢复 {count} 个参数', 'count': count})
    except Exception as e:
        return jsonify({'success': False, 'message': f'恢复失败: {e}'})


@app.route('/api/backup/delete/<filename>', methods=['POST'])
def api_delete_backup(filename):
    """删除设备内某个参数备份文件（手动或自动目录），防路径穿越"""
    sk = safe_key(filename)
    if not sk.endswith('.json'):
        return jsonify({'success': False, 'message': '仅支持删除 .json 备份文件'})
    fp = os.path.join(BACKUP_DIR, sk)
    if os.path.isfile(fp):
        try:
            os.remove(fp)
            return jsonify({'success': True, 'message': f'已删除 {sk}'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'删除失败: {e}'})
    fp = os.path.join(AUTO_BACKUP_DIR, sk)
    if os.path.isfile(fp):
        try:
            os.remove(fp)
            return jsonify({'success': True, 'message': f'已删除 {sk}'})
        except Exception as e:
            return jsonify({'success': False, 'message': f'删除失败: {e}'})
    return jsonify({'success': False, 'message': '备份文件不存在'})


@app.route('/api/param-diff', methods=['POST'])
def api_param_diff():
    """对比当前参数与指定备份文件，返回 新增/删除/修改 的差异项"""
    data = request.json or {}
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'success': False, 'message': '未指定备份文件'})
    sk = safe_key(filename)
    if not sk.endswith('.json'):
        return jsonify({'success': False, 'message': '仅支持 .json 备份文件'})
    fp = os.path.join(BACKUP_DIR, sk)
    if not os.path.isfile(fp):
        fp = os.path.join(AUTO_BACKUP_DIR, sk)
    if not os.path.isfile(fp):
        return jsonify({'success': False, 'message': '备份文件不存在'})
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            backup = json.load(f)
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取备份文件失败: {e}'})
    if not isinstance(backup, dict):
        return jsonify({'success': False, 'message': '备份文件内容无效（应为参数键值对）'})
    # 读取当前参数（与备份一致：仅非隐藏、值 <=16 字节）
    cur = {}
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
                        cur[fname] = val
                except Exception:
                    pass
    except Exception as e:
        return jsonify({'success': False, 'message': f'读取当前参数失败: {e}'})
    added, removed, changed = [], [], []
    for k in cur:
        if k not in backup:
            added.append({'key': k, 'value': cur[k]})
        elif backup[k] != cur[k]:
            changed.append({'key': k, 'old': backup[k], 'new': cur[k]})
    for k in backup:
        if k not in cur:
            removed.append({'key': k, 'value': backup[k]})
    summary = {
        'added': len(added),
        'removed': len(removed),
        'changed': len(changed),
        'same': len(cur) - len(added) - len(changed),
        'total_cur': len(cur),
        'total_backup': len(backup),
    }
    return jsonify({
        'success': True,
        'added': added,
        'removed': removed,
        'changed': changed,
        'summary': summary,
        'backup_name': sk,
    })

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


# ============= 感知数据通道（modelV2 / liveTracks / radarState）=============
# 后台线程订阅 cereal，尽力解析真实模型输出；设备离线或未行驶时为 available:false（绝不伪造数据）。
def _parse_perception(sm):
    path, lanes, boxes = [], [], []
    try:
        m = sm['modelV2']
        if m is not None:
            # 车道线：modelV2.laneLines 是 4 条折线，每条为 (x,y,z) 点序列
            try:
                for ll in m.laneLines:
                    lanes.append([[float(p.x), float(p.y), float(p.z)] for p in ll])
            except Exception:
                pass
            # 规划路径：优先 modelV2.path，回退 modelV2.position
            try:
                src = m.path if getattr(m, 'path', None) is not None and len(m.path) else getattr(m, 'position', None)
                if src is not None and len(src):
                    path = [[float(p.x), float(p.y), float(p.z)] for p in src]
            except Exception:
                pass
    except Exception:
        pass
    # 目标框：优先 liveTracks（车型/行人/自行车），回退 radarState 前车
    try:
        tr = sm['liveTracks']
        if tr is not None:
            tmap = {0: 'car', 1: 'pedestrian', 2: 'bicycle', 3: 'truck'}
            for t in tr:
                try:
                    boxes.append({
                        'type': tmap.get(int(t.t), 'car'),
                        'x': float(t.yRel), 'y': float(t.dRel), 'z': 0.0,
                        'w': 1.9, 'h': 1.5, 'd': 4.5,
                        'rel_speed': float(t.vRel), 'conf': 1.0,
                    })
                except Exception:
                    continue
    except Exception:
        pass
    if not boxes:
        try:
            rs = sm['radarState']
            if rs is not None:
                for lead in ('leadOne', 'leadTwo'):
                    L = getattr(rs, lead, None)
                    if L and getattr(L, 'status', False):
                        boxes.append({
                            'type': 'car', 'x': float(L.yRel), 'y': float(L.dRel), 'z': 0.0,
                            'w': 1.9, 'h': 1.5, 'd': 4.5,
                            'rel_speed': float(L.vRel), 'conf': 1.0,
                        })
        except Exception:
            pass
    available = bool(path or lanes or boxes)
    src = 'modelV2' if (path or lanes) else ('liveTracks' if boxes else 'none')
    return {"available": available, "source": src, "boxes": boxes, "path": path, "lane_lines": lanes}


def _perception_collector():
    """后台持续订阅 cereal，缓存最新一帧感知数据。"""
    global PERC_DATA
    try:
        from cereal.messaging import SubMaster
    except Exception as e:
        with PERC_LOCK:
            PERC_DATA = {"available": False, "source": "none", "reason": "no_cereal:%s" % e,
                         "boxes": [], "path": [], "lane_lines": []}
        print("[感知采集] 无法导入 cereal，感知接口返回 available:false（%s）" % e)
        return
    try:
        sm = SubMaster(['modelV2', 'liveTracks', 'radarState'])
    except Exception as e:
        with PERC_LOCK:
            PERC_DATA = {"available": False, "source": "none", "reason": "submaster:%s" % e,
                         "boxes": [], "path": [], "lane_lines": []}
        return
    print("[感知采集] 已启动，订阅 modelV2 / liveTracks / radarState")
    while True:
        try:
            sm.update(timeout=1.0)
            frame = _parse_perception(sm)
            with PERC_LOCK:
                PERC_DATA = frame
        except Exception:
            time.sleep(0.5)


def _parse_events(sm):
    """从 carState 解析原车动作，返回状态字典。
    注：倒车(gearShifter 是 capnp 枚举，非字符串 'reverse')与减速(controlsState.decelFor*
    字段在 cpv9-dev 分支不一定存在)两类信号在该分支不可靠，已移除，避免误判不触发。
    仅保留转向灯与 ACC——这两类字段跨 openpilot 版本稳定。"""
    st = {"left_blinker": False, "right_blinker": False, "acc_enabled": False}
    try:
        cs = sm['carState']
        if cs is not None:
            st['left_blinker'] = bool(getattr(cs, 'leftBlinker', False))
            st['right_blinker'] = bool(getattr(cs, 'rightBlinker', False))
            try:
                st['acc_enabled'] = bool(getattr(cs, 'cruiseState', None) and getattr(cs.cruiseState, 'enabled', False))
            except Exception:
                st['acc_enabled'] = False
    except Exception:
        pass
    return st


def _event_collector():
    """后台持续订阅 cereal 原车信号，检测状态边沿（上升/下降沿）并缓存最新事件。"""
    global EVENT_DATA
    try:
        from cereal.messaging import SubMaster
    except Exception as e:
        with EVENT_LOCK:
            EVENT_DATA = {"available": False, "reason": "no_cereal:%s" % e,
                          "left_blinker": False, "right_blinker": False, "acc_enabled": False,
                          "last_event": {"type": None, "seq": 0, "ts": 0.0}}
        print("[事件采集] 无法导入 cereal，事件接口返回 available:false（%s）" % e)
        return
    try:
        sm = SubMaster(['carState'])
    except Exception as e:
        with EVENT_LOCK:
            EVENT_DATA = {"available": False, "reason": "submaster:%s" % e,
                          "left_blinker": False, "right_blinker": False, "acc_enabled": False,
                          "last_event": {"type": None, "seq": 0, "ts": 0.0}}
        return
    print("[事件采集] 已启动，订阅 carState")
    seq = 0
    prev = {}
    while True:
        try:
            sm.update(timeout=1.0)
            st = _parse_events(sm)
            edges = []
            if st.get('left_blinker') and not prev.get('left_blinker'):
                edges.append('turn_left')
            if st.get('right_blinker') and not prev.get('right_blinker'):
                edges.append('turn_right')
            if st.get('acc_enabled') and not prev.get('acc_enabled'):
                edges.append('acc_on')
            if prev.get('acc_enabled') and not st.get('acc_enabled'):
                edges.append('acc_off')
            prev = st
            with EVENT_LOCK:
                d = dict(EVENT_DATA)
                d.update(st)
                d['available'] = True
                d['reason'] = 'ok'
                if edges:
                    seq += 1
                    d['last_event'] = {"type": edges[-1], "seq": seq, "ts": time.time()}
                EVENT_DATA = d
        except Exception:
            time.sleep(0.5)


@app.route('/api/perception')
def api_perception():
    with PERC_LOCK:
        return jsonify(PERC_DATA)


# ===== 原车动作事件 + 语音互动 =====
VALID_VOICE = {'turn_left', 'turn_right', 'acc_on', 'acc_off'}
VOICE_TEXT = {
    'turn_left': '正在左转向',
    'turn_right': '正在右转向',
    'acc_on': 'OP智能驾驶已激活',
    'acc_off': '退出智能驾驶',
}


@app.route('/api/car_events')
def api_car_events():
    with EVENT_LOCK:
        return jsonify(EVENT_DATA)


@app.route('/api/voice/test_event', methods=['POST'])
def api_voice_test_event():
    """注入一个测试事件（绕过实车 cereal 信号），用于在设备 shell 用 curl 模拟转向/ACC
    动作，验证前端轮询→播放链路是否通畅。用法：
      curl -X POST http://localhost:5588/api/voice/test_event?type=turn_left
    type 取值：turn_left / turn_right / acc_on / acc_off
    """
    t = request.form.get('type') or request.args.get('type')
    valid = sorted(VALID_VOICE)
    if t not in VALID_VOICE:
        return jsonify({'success': False, 'message': '未知事件类型，可选: %s' % ','.join(valid)})
    global EVENT_DATA
    with EVENT_LOCK:
        d = dict(EVENT_DATA)
        cur_seq = (d.get('last_event') or {}).get('seq', 0) + 1
        d['last_event'] = {'type': t, 'seq': cur_seq, 'ts': time.time(), 'test': True}
        EVENT_DATA = d
    return jsonify({'success': True, 'type': t, 'seq': cur_seq,
                    'message': '已注入测试事件，前端将在 ~800ms 内轮询到并播报（需语音开关已开、浏览器在前台且音频已解锁）'})


@app.route('/api/voice/list')
def api_voice_list():
    out = {}
    for t in VALID_VOICE:
        out[t] = os.path.isfile(os.path.join(VOICE_DIR, t + '.mp3'))
    return jsonify(out)


@app.route('/api/voice/<t>')
def api_voice_file(t):
    if t not in VALID_VOICE:
        return ('invalid type', 404)
    p = os.path.join(VOICE_DIR, t + '.mp3')
    if not os.path.isfile(p):
        return ('', 404)
    # 不用 send_file：其 cache_timeout 参数在 Flask 2.0+ 已移除会抛 TypeError 导致 500。
    # 手动读文件 + Response 返回，跨 Flask 版本最稳，前端用 fetch+Blob 播放不需要 Range。
    try:
        with open(p, 'rb') as f:
            data = f.read()
    except Exception:
        return ('read error', 500)
    resp = Response(data, mimetype='audio/mpeg')
    resp.headers['Accept-Ranges'] = 'bytes'
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['Content-Length'] = str(len(data))
    return resp


@app.route('/api/voice/upload', methods=['POST'])
def api_voice_upload():
    t = request.form.get('type') or request.args.get('type')
    if t not in VALID_VOICE:
        return jsonify({'success': False, 'message': '未知的语音类型'})
    f = request.files.get('file')
    if not f:
        return jsonify({'success': False, 'message': '未收到文件'})
    fn = (f.filename or '').lower()
    ext = os.path.splitext(fn)[1]
    if ext not in ('.mp3', '.wav', '.ogg', '.m4a'):
        return jsonify({'success': False, 'message': '仅支持 mp3 / wav / ogg / m4a'})
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        dest = os.path.join(VOICE_DIR, t + '.mp3')
        f.save(dest)
    except Exception as e:
        return jsonify({'success': False, 'message': '保存失败：%s' % e})
    return jsonify({'success': True, 'type': t, 'message': '语音已替换'})


@app.route('/api/voice/reset', methods=['POST'])
def api_voice_reset():
    t = request.form.get('type') or request.args.get('type')
    if t not in VALID_VOICE:
        return jsonify({'success': False, 'message': '未知的语音类型'})
    p = os.path.join(VOICE_DIR, t + '.mp3')
    if os.path.isfile(p):
        try:
            os.remove(p)
        except Exception as e:
            return jsonify({'success': False, 'message': '删除失败：%s' % e})
    return jsonify({'success': True, 'type': t, 'message': '已恢复默认语音'})


@app.route('/api/voice/export')
def api_voice_export():
    """打包所有已上传替换的语音 + manifest，方便备份与分享给朋友。"""
    bins = {}  # type -> 二进制
    manifest = {'version': 1, 'types': []}
    for t in sorted(VALID_VOICE):
        p = os.path.join(VOICE_DIR, t + '.mp3')
        replaced = os.path.isfile(p)
        manifest['types'].append({
            'type': t,
            'text': VOICE_TEXT.get(t, ''),
            'replaced': replaced,
        })
        if replaced:
            try:
                with open(p, 'rb') as f:
                    data = f.read()
                if data:
                    bins[t] = data
            except Exception:
                pass
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for t, data in bins.items():
            z.writestr(t + '.mp3', data)
        z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    buf.seek(0)
    resp = send_file(buf, mimetype='application/zip')
    resp.headers['Content-Disposition'] = 'attachment; filename="c3_voice_pack.zip"'
    return resp


@app.route('/api/voice/import', methods=['POST'])
def api_voice_import():
    """导入朋友分享的语音包 zip（仅接受 6 类合法语音，防目录穿越）。"""
    f = request.files.get('file')
    if not f:
        return jsonify({'success': False, 'message': '未收到文件'})
    fn = (f.filename or '').lower()
    if not fn.endswith('.zip'):
        return jsonify({'success': False, 'message': '仅支持 .zip 语音包'})
    try:
        raw = f.read()
        imported = 0
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            for name in z.namelist():
                base = os.path.basename(name)  # 丢弃路径，防穿越
                if not base.lower().endswith('.mp3'):
                    continue
                t = base[:-4]
                if t not in VALID_VOICE:
                    continue
                try:
                    content = z.read(name)
                except Exception:
                    continue
                if not content or len(content) > 5 * 1024 * 1024:  # 单文件 5MB 上限
                    continue
                os.makedirs(VOICE_DIR, exist_ok=True)
                with open(os.path.join(VOICE_DIR, t + '.mp3'), 'wb') as out:
                    out.write(content)
                imported += 1
    except zipfile.BadZipFile:
        return jsonify({'success': False, 'message': '文件不是有效的语音包 zip'})
    except Exception as e:
        return jsonify({'success': False, 'message': '导入失败：%s' % e})
    return jsonify({'success': True, 'imported': imported, 'message': '已导入 %d 条语音' % imported})


# ============= 局域网扫描 / 设备发现 =============
# 纯 Python socket 扫描，不依赖 nmap；并发 + 整体超时，避免长时间阻塞请求线程。
import socket as _lsocket

SCAN_PORTS = [5588, 5555, 5037, 80, 22, 5050, 8080, 8000, 5000, 8888, 9000, 9090]


def _get_own_ips():
    """返回设备自身所有 IPv4 私网地址列表（优先 hostname -I，回退 ip addr）。"""
    out = []
    try:
        raw = run_cmd("hostname -I 2>/dev/null")
        for p in raw.split():
            p = p.strip()
            if re.match(r'^\d+\.\d+\.\d+\.\d+$', p):
                out.append(p)
    except Exception:
        pass
    if not out:
        try:
            raw = run_cmd("ip -4 addr show 2>/dev/null | grep -oP 'inet \\K[\\d.]+'")
            for p in raw.split():
                if p != '127.0.0.1' and re.match(r'^\d+\.\d+\.\d+\.\d+$', p):
                    out.append(p)
        except Exception:
            pass
    return out


def _is_private(ip):
    if ip.startswith('192.168.'):
        return True
    if ip.startswith('10.'):
        return True
    if ip.startswith('172.'):
        b = ip.split('.')[1] if len(ip.split('.')) > 1 else '0'
        try:
            return 16 <= int(b) <= 31
        except ValueError:
            return False
    return False


def _own_subnets():
    """返回本机私网地址对应的 /24 网段前缀列表，如 ['192.168.5']（去重保序）。"""
    subs = []
    seen = set()
    for ip in _get_own_ips():
        if _is_private(ip):
            parts = ip.split('.')
            if len(parts) == 4:
                s = '.'.join(parts[:3])
                if s not in seen:
                    seen.add(s)
                    subs.append(s)
    return subs


def _scan_lan(subnets, ports=None, per_host_timeout=0.15, overall_timeout=15):
    """并发扫描给定网段下所有主机的开放端口，返回设备列表 [{ip,ports,is_self}]。"""
    if ports is None:
        ports = SCAN_PORTS
    import threading
    hosts = []
    for s in subnets:
        for i in range(1, 255):
            hosts.append('%s.%d' % (s, i))
    found = []
    found_lock = threading.Lock()
    stop = threading.Event()
    deadline = time.time() + overall_timeout
    own = set(_get_own_ips())

    def worker(ip):
        if stop.is_set():
            return
        open_ports = []
        for pt in ports:
            if stop.is_set():
                break
            try:
                sk = _lsocket.socket(_lsocket.AF_INET, _lsocket.SOCK_STREAM)
                sk.settimeout(per_host_timeout)
                if sk.connect_ex((ip, pt)) == 0:
                    open_ports.append(pt)
                sk.close()
            except Exception:
                pass
        if open_ports:
            with found_lock:
                found.append({'ip': ip, 'ports': sorted(open_ports)})

    threads = []
    for ip in hosts:
        if time.time() > deadline:
            stop.set()
            break
        t = threading.Thread(target=worker, args=(ip,))
        t.daemon = True
        t.start()
        threads.append(t)
        if len(threads) >= 80:
            for tt in threads:
                tt.join()
            threads = []
    for tt in threads:
        tt.join()
    for f in found:
        f['is_self'] = f['ip'] in own
    # 本机优先，其余按 IP 升序
    found.sort(key=lambda x: (not x['is_self'], x['ip']))
    return found


@app.route('/api/lan_info')
def api_lan_info():
    """返回本机 IP 与可扫描网段，便于前端展示「C3 当前地址」并作为扫描依据。"""
    ips = _get_own_ips()
    subnets = _own_subnets()
    return jsonify({'success': True, 'ips': ips, 'subnets': subnets, 'scan_ports': SCAN_PORTS})


@app.route('/api/scan_lan', methods=['GET', 'POST'])
def api_scan_lan():
    """扫描本机所在私网网段，列出在线设备（IP + 开放端口）。
    用途：①确认 C3 自身当前 IP（每次开机都变）；②排查平板/手机是否在同一网段、端口是否可达，
    无需手动猜 IP。纯 Python socket 扫描，不依赖 nmap；并发 + 整体超时，避免长时间阻塞。"""
    subnets = _own_subnets()
    if not subnets:
        return jsonify({'success': False, 'message': '未检测到私网地址（C3 可能未连接 Wi-Fi）', 'devices': [], 'subnets': []})
    try:
        devices = _scan_lan(subnets)
        return jsonify({'success': True, 'subnets': subnets, 'count': len(devices), 'devices': devices})
    except Exception as e:
        return jsonify({'success': False, 'message': '扫描失败: %s' % e, 'devices': [], 'subnets': subnets})


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
    # 启动感知采集线程（仅在设备端 cereal 可用时真正工作；不可用时接口返回 available:false）
    threading.Thread(target=_perception_collector, daemon=True).start()
    # 启动原车事件采集线程（转向 / ACC / 倒车 / 减速；无 cereal 时 available:false，不播报）
    threading.Thread(target=_event_collector, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)