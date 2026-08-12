#!/usr/local/venv/bin/python
# 生成打开工具箱二维码 -> /data/c3_toolbox/qr.png（按当前局域网 IP:5588，实时刷新）
import qrcode, zlib, struct, subprocess, time, os

QR_PATH = "/data/c3_toolbox/qr.png"
PORT = 5588
SCALE = 10
BORDER = 4
POLL = 10  # 每 10 秒检测一次网段变化

def _ok(ip):
    return bool(ip) and "." in ip and not ip.startswith("127.") and not ip.startswith("169.254.")

def _is_cellular(dev):
    return dev.startswith(("rmnet", "ccmni", "wwan", "ppp")) if dev else False

def _is_virtual(iface):
    return iface.startswith(("docker", "br-", "veth", "lo", "tun", "tailscale", "wg"))

def _iface_ips():
    res = []
    try:
        out = subprocess.check_output(["ip", "-o", "-4", "addr", "show"],
                                      stderr=subprocess.DEVNULL).decode()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[2] == "inet":
                iface = parts[1].split('@')[0]
                ip = parts[3].split('/')[0]
                res.append((iface, ip))
    except Exception:
        pass
    return res

def get_lan_ip():
    # 1) 默认路由出接口 IP（手机与 C3 同网时即此 IP；切网后随之变化）
    try:
        out = subprocess.check_output(["ip", "route", "get", "1.1.1.1"],
                                      stderr=subprocess.DEVNULL).decode()
        parts = out.split()
        dev = parts[parts.index("dev") + 1] if "dev" in parts else ""
        src = parts[parts.index("src") + 1] if "src" in parts else ""
        if _ok(src) and not _is_cellular(dev):
            return src
    except Exception:
        pass
    # 2) 枚举接口: 优先 wlan*/ap*/eth* 等无线/有线, 避开蜂窝/虚拟网卡
    for iface, ip in _iface_ips():
        if _ok(ip) and not _is_cellular(iface) and not _is_virtual(iface):
            return ip
    # 3) 兜底 hostname -I, 跳过常见虚拟网段
    try:
        for ip in subprocess.check_output(["hostname", "-I"]).decode().split():
            if _ok(ip) and not ip.startswith(("172.17.", "172.18.", "172.19.", "172.20.", "172.21.", "172.22.")):
                return ip
    except Exception:
        pass
    return "192.168.5.34"

def write_png(path, matrix, scale=SCALE, border=BORDER):
    size = len(matrix)
    dim = size + 2 * border
    full = dim * scale
    raw = bytearray()
    for r in range(full):
        raw.append(0)
        mr = r // scale - border
        for cc in range(full):
            mc = cc // scale - border
            black = (0 <= mr < size and 0 <= mc < size and matrix[mr][mc])
            v = 0 if black else 255
            raw += bytes((v, v, v))
    def chunk(typ, data):
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", full, full, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))

def gen():
    ip = get_lan_ip()
    url = f"http://{ip}:{PORT}"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=0)
    qr.add_data(url)
    qr.make(fit=True)
    write_png(QR_PATH, qr.get_matrix())
    print(f"[gen_qr] {url} -> {QR_PATH}")

if __name__ == "__main__":
    last = ""
    while True:
        try:
            ip = get_lan_ip()
            # IP 变化 或 二维码文件丢失 -> 立即重生成
            if ip != last or not os.path.exists(QR_PATH):
                gen()
                last = ip
        except Exception as e:
            print("[gen_qr] err:", e)
        time.sleep(POLL)
