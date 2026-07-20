#!/usr/bin/env python3
"""C3 工具箱 - 实时驾驶数据收集器（Dashy 风格，精简只读版）

订阅 openpilot cereal 总线，把车速 / 车道线 / 前车 / 温度等格式化为 JSON，
每 ~100ms 原子写入 /tmp/dashy_state.json，供工具箱前端 Canvas HUD 读取。

设计要点：
- 仅读取 cereal，绝不写入任何参数。
- 用 openpilot 自带的 python 运行（由工具箱探测后拉起），绕开工具箱 venv 的编译 .so ABI 问题。
- cereal 是 openpilot / carrot / sunnypilot / frogpilot 通用的消息总线，五分支都适用。
"""
import sys, os, time, json, threading
import numpy as np

OPENPILOT_DIR = "/data/openpilot"
STATE = "/tmp/dashy_state.json"
TMP = "/tmp/dashy_state.tmp.json"
LOG = "/tmp/dashy_state.log"
SCREEN_JPG = "/tmp/screen.jpg"  # 屏幕帧（来自 cereal uiDebug.frame，JPEG），供投屏使用


def safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


GEAR = {0: '?', 1: 'P', 2: 'R', 3: 'N', 4: 'D', 5: 'B', 6: 'S', 7: 'L', 8: 'E'}


def gear_name(gs):
    try:
        if hasattr(gs, 'name'):
            return str(gs.name)
        return GEAR.get(int(gs), '?')
    except Exception:
        return '?'


def downsample_pts(pts, n=17):
    out = []
    ln = len(pts)
    if ln == 0:
        return out
    step = max(1, ln // n)
    for i in range(0, ln, step):
        p = pts[i]
        out.append([round(float(p.x), 2), round(float(p.y), 2)])
    return out


# ===== UI 屏幕流采集（VisionIpc，comma connect 同款机制）=====
# comma 设备上的「屏幕画面」= selfdrive/ui 进程通过 VisionIpc 发布的 uiDebug 像素流（合成后的
# UI 屏幕，含车速/车道线/报警等 HUD 叠加层），不是 cereal 的 uiDebug.frame（那是相机/metadata）。
# 故用 VisionIpcClient 直接订阅该像素流，解码成 JPEG 写 /tmp/screen.jpg 供投屏。

def _vipc_diag(msg):
    try:
        path = "/tmp/screen_vipc.log"
        line = "[%s] %s\n" % (time.strftime("%H:%M:%S"), msg)
        # 日志超过 50KB 时只保留末尾 200 行，避免无限增长
        if os.path.exists(path) and os.path.getsize(path) > 50000:
            try:
                with open(path) as f:
                    tail = f.read().splitlines()[-200:]
                with open(path, "w") as f:
                    f.write("\n".join(tail) + "\n")
            except Exception:
                pass
        with open(path, "a") as f:
            f.write(line)
    except Exception:
        pass


def _battr(buf, name, default=0):
    try:
        v = getattr(buf, name, default)
        if callable(v):
            v = v()
        return int(v)
    except Exception:
        return default


def _decode_rgb(buf, is_rgb):
    """从 VisionBuf 解码出 HxWx3 uint8 RGB。is_rgb=True 表示 RGB 流，否则 NV12 流。"""
    w = _battr(buf, "width"); h = _battr(buf, "height")
    if not w or not h:
        return None
    try:
        raw = buf.data
        if isinstance(raw, (bytes, bytearray, memoryview)):
            data = np.frombuffer(raw, dtype=np.uint8)
        elif isinstance(raw, np.ndarray):
            data = raw.ravel().view(np.uint8) if raw.dtype != np.uint8 else raw.ravel()
        else:
            data = np.frombuffer(bytes(raw), dtype=np.uint8)
    except Exception:
        return None
    try:
        if is_rgb:
            return data.reshape(h, w, 3).copy()
        stride = _battr(buf, "stride") or w
        uv_off = _battr(buf, "uv_offset")
        if not uv_off:
            uv_off = stride * (((h // 2) + 15) // 16 * 16)
        y = data[:uv_off].reshape(-1, stride)[:h, :w]
        uv_plane = stride * (((h // 2) + 15) // 16 * 16)
        uv = data[uv_off:uv_off + uv_plane]
        u = uv[::2].reshape(-1, stride // 2)[:h // 2, :w // 2]
        v = uv[1::2].reshape(-1, stride // 2)[:h // 2, :w // 2]
        u2 = u.repeat(2, 0).repeat(2, 1)[:h, :w]
        v2 = v.repeat(2, 0).repeat(2, 1)[:h, :w]
        yuv = np.dstack((y.astype(np.int16), u2.astype(np.int16), v2.astype(np.int16))).astype(np.int16)
        yuv[:, :, 1:] -= 128
        mat = np.array([[1.0, 1.0, 1.0],
                        [0.0, -0.39465, 2.03211],
                        [1.13983, -0.58060, 0.0]])
        rgb = np.dot(yuv, mat).clip(0, 255).astype(np.uint8)
        return rgb
    except Exception as e:
        _vipc_diag("decode error: %s" % e)
        return None


def _rgb_to_jpg(rgb):
    try:
        from PIL import Image
        import io
        out = io.BytesIO()
        Image.fromarray(rgb).save(out, "JPEG", quality=82)
        return out.getvalue()
    except Exception:
        pass
    try:
        import cv2
        ok, buf = cv2.imencode(".jpg", rgb[..., ::-1])
        if ok:
            return buf.tobytes()
    except Exception:
        pass
    return None


def _recv_timeout(client, timeout):
    res = [None]

    def _r():
        try:
            res[0] = client.recv()
        except Exception:
            res[0] = None
    t = threading.Thread(target=_r, daemon=True)
    t.start()
    t.join(timeout)
    return res[0]


def screen_loop():
    """独立线程：用 VisionIpc 订阅 UI 屏幕流（comma connect 同款机制），把合成后的 UI 屏幕
    （含车速/车道线/报警等 HUD 叠加层）解码成 JPEG 写到 /tmp/screen.jpg，供投屏使用。

    真正的「屏幕画面」在 comma 设备上是 selfdrive/ui 进程通过 VisionIpc 发布的 uiDebug 像素流，
    而不是 cereal 的 uiDebug.frame（那是相机/metadata，之前误抓到摄像头画面就源于此）。
    本线程直接抓 VisionIpc 像素流。仅读取，不写参数。失败不影响 HUD 主循环。诊断写入 /tmp/screen_vipc.log。
    """
    _vipc_diag("screen_loop 启动 (python=%s)" % (sys.version.split()[0]))
    try:
        try:
            from cereal.visionipc import VisionIpcClient, VisionStreamType
        except Exception:
            from msgq.visionipc import VisionIpcClient, VisionStreamType
    except Exception as e:
        _vipc_diag("import VisionIpc 失败: %s" % e)
        return
    _vipc_diag("VisionIpc 导入成功")

    servers = ["uiDebug", "ui", "", "camerad", "roadCameraState"]
    # 候选 stream 类型（优先 UI 类，再枚举设备实际存在的全部类型以免漏掉分支自定义名）
    rgb_flags = {"VISION_STREAM_RGB_UI_BACK": True, "VISION_STREAM_RGB_UI_FRONT": True,
                 "VISION_STREAM_UI_BACK": False, "VISION_STREAM_UI_FRONT": False}
    for s in [x for x in dir(VisionStreamType) if not x.startswith('_')]:
        rgb_flags.setdefault(s, False)
    cands = []
    for sn, is_rgb in rgb_flags.items():
        try:
            cands.append((sn, getattr(VisionStreamType, sn), is_rgb))
        except Exception:
            pass
    if not cands:
        _vipc_diag("无可用 stream 枚举（VisionStreamType 为空）")
        return
    _vipc_diag("候选 stream 数=%d: %s" % (len(cands), ",".join(s for s, _, _ in cands)))

    last = None
    cycle = 0
    while True:
        cycle += 1
        connected = False
        for srv in servers:
            for sn, st, is_rgb in cands:
                try:
                    client = VisionIpcClient(srv, st, True)
                    try:
                        client.connect()
                    except TypeError:
                        client.connect(True)
                    buf0 = _recv_timeout(client, 3)
                    if buf0 is None:
                        try:
                            client.stop()
                        except Exception:
                            pass
                        continue
                    w = _battr(buf0, "width"); h = _battr(buf0, "height")
                    fmt = getattr(buf0, "format", None)
                    rgb0 = _decode_rgb(buf0, is_rgb)
                    if rgb0 is None:
                        try:
                            client.stop()
                        except Exception:
                            pass
                        _vipc_diag("解码失败 %s/%s %dx%d fmt=%s" % (srv, sn, w, h, fmt))
                        continue
                    _vipc_diag("已连接 %s/%s %dx%d fmt=%s -> 写 %s" % (srv, sn, w, h, fmt, SCREEN_JPG))
                    connected = True
                    last = None
                    while True:
                        try:
                            buf = _recv_timeout(client, 1)
                            if buf is None:
                                break  # 超时/断开，回到外层重连
                            rgb = _decode_rgb(buf, is_rgb)
                            if rgb is None:
                                continue
                            jpg = _rgb_to_jpg(rgb)
                            if jpg and jpg != last:
                                last = jpg
                                with open(SCREEN_JPG, "wb") as fh:
                                    fh.write(jpg)
                        except Exception:
                            break
                    try:
                        client.stop()
                    except Exception:
                        pass
                    _vipc_diag("%s/%s 断开，回到重连" % (srv, sn))
                    break
                except Exception as e:
                    _vipc_diag("异常 %s/%s: %s" % (srv, sn, e))
            if connected:
                break
        if not connected:
            # 每 ~10s（20 轮）汇总一次，避免刷屏
            if cycle % 20 == 1:
                _vipc_diag("扫描 %d 源均无可用 UI 屏幕流，重试中（请确保 openpilot UI 正在设备屏幕运行）" % (len(cands) * len(servers)))
        time.sleep(0.5)


def main():
    if OPENPILOT_DIR not in sys.path:
        sys.path.insert(0, OPENPILOT_DIR)
    from cereal.messaging import SubMaster
    import threading

    TOPICS = ["carState", "modelV2", "radarState", "liveCalibration",
              "deviceState", "selfdriveState", "liveMapData", "controlsState"]
    sm = SubMaster(TOPICS)

    # 启动屏幕帧收集线程（独立，失败不影响 HUD）
    try:
        threading.Thread(target=screen_loop, daemon=True).start()
    except Exception:
        pass

    def build_default():
        return {"offroad": True, "engaged": False, "noData": False,
                "vEgo": 0.0, "aEgo": 0.0, "steer": 0.0, "gear": "?",
                "blinkerL": False, "blinkerR": False, "cruise": False,
                "cruiseSpeed": 0.0, "gas": 0.0, "brake": 0.0,
                "lanes": [], "path": [], "lead": None,
                "calStatus": 0, "cpuTemp": 0.0, "gpuTemp": 0.0, "freeSpace": 0.0,
                "experimental": False, "speedLimit": None, "isMetric": True}

    while True:
        sm.update(100)
        st = build_default()

        # 单位偏好（IsMetric 参数）
        try:
            ip = "/data/params/d/IsMetric"
            if os.path.exists(ip):
                st["isMetric"] = open(ip).read().strip() in ("1", "true", "True")
        except Exception:
            pass

        if sm.updated["deviceState"]:
            ds = sm["deviceState"]
            cpus = safe(lambda: list(ds.cpuTemp), [0]) or [0]
            st["cpuTemp"] = round(max(cpus), 1)
            st["gpuTemp"] = round(safe(lambda: float(ds.gpuTemp), 0.0), 1)
            st["freeSpace"] = round(safe(lambda: float(ds.freeSpace), 0.0), 2)

        if sm.updated["selfdriveState"]:
            sds = sm["selfdriveState"]
            st["engaged"] = bool(safe(lambda: sds.enabled, False))
            st["experimental"] = bool(safe(lambda: sds.experimentalMode, False))

        if sm.updated["carState"]:
            cs = sm["carState"]
            st["vEgo"] = round(safe(lambda: float(cs.vEgo), 0.0), 2)
            st["aEgo"] = round(safe(lambda: float(cs.aEgo), 0.0), 2)
            st["steer"] = round(safe(lambda: float(cs.steeringAngleDeg), 0.0), 1)
            st["gear"] = gear_name(safe(lambda: cs.gearShifter, '?'))
            st["blinkerL"] = bool(safe(lambda: cs.leftBlinker, False))
            st["blinkerR"] = bool(safe(lambda: cs.rightBlinker, False))
            st["cruise"] = bool(safe(lambda: cs.cruiseState.enabled, False))
            st["cruiseSpeed"] = round(safe(lambda: float(cs.cruiseState.speed), 0.0), 2)
            st["gas"] = round(safe(lambda: float(cs.gas), 0.0), 2)
            st["brake"] = round(safe(lambda: float(cs.brake), 0.0), 2)
            st["offroad"] = False

        if sm.updated["modelV2"]:
            mv = sm["modelV2"]
            try:
                st["lanes"] = [downsample_pts(line) for line in mv.laneLines]
            except Exception:
                st["lanes"] = []
            try:
                st["path"] = downsample_pts(mv.position, 17)
            except Exception:
                st["path"] = []

        if sm.updated["radarState"]:
            try:
                ld = sm["radarState"].leadOne
                d = ld.dRel
                if d is not None and 0 < d < 300:
                    st["lead"] = {"dRel": round(float(d), 1),
                                  "yRel": round(float(ld.yRel), 1),
                                  "vRel": round(float(ld.vRel), 1)}
            except Exception:
                st["lead"] = None

        if sm.updated["liveCalibration"]:
            try:
                st["calStatus"] = int(sm["liveCalibration"].calStatus)
            except Exception:
                pass

        if sm.updated["liveMapData"]:
            try:
                sl = sm["liveMapData"].speedLimit
                if sl is not None:
                    st["speedLimit"] = round(float(sl), 1)
            except Exception:
                pass

        try:
            with open(TMP, "w") as f:
                json.dump(st, f)
            os.replace(TMP, STATE)
        except Exception:
            pass
        time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            with open(LOG, "w") as f:
                f.write("dashy_collector 启动失败: %s\n" % e)
            with open(STATE, "w") as f:
                json.dump({"offroad": True, "noData": True, "error": str(e)}, f)
        except Exception:
            pass
        time.sleep(8)
