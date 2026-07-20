#!/usr/bin/env python3
# VisionIpc 屏幕流穷举探测（诊断用，不改任何参数）
# 用法：python3 vipc_probe.py
# 作用：枚举设备上所有 VisionStreamType，对每个尝试用 uiDebug/ui 等 server 名连接并取一帧，
#       打印出哪些 (server, stream) 真正有画面（含分辨率与像素格式）。据此精确锁定投屏源。
import sys, time, threading

sys.path.insert(0, "/data/openpilot")

try:
    from cereal.visionipc import VisionIpcClient, VisionStreamType
except Exception:
    try:
        from msgq.visionipc import VisionIpcClient, VisionStreamType
    except Exception as e:
        print("IMPORT_FAIL:", repr(e))
        sys.exit(1)

print("PY:", sys.version.split()[0])

sts = [s for s in dir(VisionStreamType) if not s.startswith('_')]
print("STREAM_TYPES:", sts)

# 优先 UI 相关 server，其次常见名；范围小以保证快速出结果
SERVERS = ["uiDebug", "ui", ""]
# UI 相关的 stream 类型优先探测
ui_like = [s for s in sts if "UI" in s]
other = [s for s in sts if "UI" not in s]
ordered = ui_like + other

def with_timeout(fn, t):
    res = [None, False, False, None]  # [result, raised, alive, err]
    def _r():
        try:
            res[0] = fn()
        except Exception as e:
            res[1] = True
            res[3] = str(e)
    th = threading.Thread(target=_r, daemon=True)
    th.start()
    th.join(t)
    return res[0], res[1], th.is_alive(), res[3]

found = 0
for st_name in ordered:
    try:
        st = getattr(VisionStreamType, st_name)
    except Exception:
        continue
    for srv in SERVERS:
        try:
            try:
                c = VisionIpcClient(srv, st, True)
            except TypeError:
                c = VisionIpcClient(srv, st)
        except Exception:
            continue
        def do_connect():
            try:
                return c.connect()
            except TypeError:
                try:
                    return c.connect(True)
                except Exception:
                    return None
            except Exception:
                return None
        _, raised, alive, err = with_timeout(do_connect, 1.5)
        if alive:
            # connect 阻塞 = stream 存在但暂未出帧（多为 offroad 下不渲染）
            try:
                c.stop()
            except Exception:
                pass
            continue
        if raised:
            continue
        buf, r2, a2, e2 = with_timeout(lambda: c.recv(), 1.5)
        if a2:
            pass  # recv 阻塞，跳过
        elif buf is not None:
            try:
                w = getattr(buf, "width", "?")
                h = getattr(buf, "height", "?")
                fmt = getattr(buf, "format", "?")
                d = getattr(buf, "data", None)
                dl = len(d) if d is not None else 0
                print("CONNECTED srv=%r stream=%s %sx%s fmt=%s datalen=%s"
                      % (srv, st_name, w, h, fmt, dl))
                found += 1
            except Exception as e:
                print("CONNECTED srv=%r stream=%s attr_err=%s" % (srv, st_name, e))
        try:
            c.stop()
        except Exception:
            pass

print("DONE found=%d" % found)
if found == 0:
    print("提示：未找到任何可用屏幕流。可能原因：① openpilot UI 未在设备屏幕运行（offroad 黑屏不渲染）；"
          "② 本分支 UI 发布到的 server/stream 名不在探测列表。请把上方 STREAM_TYPES 列表发我。")
