#!/usr/bin/env python3
"""
C3 设备工具箱 - 设备本地版 v2 (Carrotpilot cpv9-dev)
=====================================================
直接运行在 C3 设备上，浏览器访问 http://设备IP:5588 即可
"""

import json, os, sys, time, subprocess, urllib.request, tarfile, io, threading, traceback

try:
    from flask import Flask, request, jsonify, send_file, send_from_directory
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
VERSION = "1.0.8"
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


def discover_latest_version():
    """通过 jsdelivr 数据 API 实时发现最新版本号（tag）。
    该 API 属 jsdelivr 域、国内可达，且实时反映最新 git tag，不受 CDN 文件缓存影响。
    返回如 '1.0.7'；失败返回 None。"""
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
            # 用列表参数调用，避免中文文件名在 shell 中被转义出错
            proc = subprocess.run(['tar', '-zcvf', out_path, '/data/openpilot'],
                                  capture_output=True, text=True, timeout=1800)
            if proc.returncode == 0 and os.path.isfile(out_path):
                sz = os.path.getsize(out_path)
                OP_TASKS[task_id] = {
                    'status': 'done', 'done': True,
                    'message': f'备份完成: {fname} ({sz/1024/1024:.0f} MB)',
                    'filename': fname, 'size': sz,
                }
            else:
                err = (proc.stderr or '未知错误')[-600:]
                OP_TASKS[task_id] = {'status': 'error', 'done': True, 'message': f'备份失败: {err}'}
        except subprocess.TimeoutExpired:
            OP_TASKS[task_id] = {'status': 'error', 'done': True, 'message': '备份超时（>30 分钟）'}
        except Exception as e:
            OP_TASKS[task_id] = {'status': 'error', 'done': True, 'message': f'备份异常: {e}'}
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
        # 先停 openpilot，避免运行中文件被覆盖导致不一致（不影响工具箱本身）
        subprocess.run("pkill -f manager.py", shell=True, timeout=5)
        time.sleep(2)
        proc = subprocess.run(['tar', '-zxvf', pkg_path, '-C', '/'],
                              capture_output=True, text=True, timeout=1800)
        try:
            os.remove(pkg_path)  # 上传的临时包用完后清理
        except Exception:
            pass
        if proc.returncode == 0:
            msg = '恢复完成: ' + os.path.basename(pkg_path)
            if reboot_after:
                msg += '，设备即将重启...'
                OP_TASKS[task_id] = {'status': 'done', 'done': True, 'message': msg}
                time.sleep(1)
                subprocess.Popen(["sudo", "reboot"])
            else:
                OP_TASKS[task_id] = {
                    'status': 'done', 'done': True,
                    'message': msg + '（建议手动重启设备使 openpilot 重新加载）',
                }
        else:
            err = (proc.stderr or '未知错误')[-600:]
            OP_TASKS[task_id] = {'status': 'error', 'done': True, 'message': f'恢复失败: {err}'}
    except subprocess.TimeoutExpired:
        OP_TASKS[task_id] = {'status': 'error', 'done': True, 'message': '恢复超时（>30 分钟）'}
    except Exception as e:
        OP_TASKS[task_id] = {'status': 'error', 'done': True, 'message': f'恢复异常: {e}'}


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
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)