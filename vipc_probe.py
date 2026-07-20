#!/usr/bin/env python3
# VisionIpc 屏幕流穷举探测（诊断用，不改任何参数）
# 用法：python3 vipc_probe.py
# 关键点：
#  - 多进程：每个 (server,stream) 在独立子进程里 connect+recv，父进程超时即 terminate，
#    能真正干掉 VisionIpc C 扩展的阻塞 connect（Python 线程杀不掉它，旧版因此卡死刷屏）。
#  - 屏蔽子进程 fd 1/2 到 /dev/null，避免 C 扩展的 "VisionIpcClient connecting" 刷屏。
#  - 优先非阻塞 connect(False)；不支持时回退阻塞 connect（仍由父进程超时 terminate）。
#  - 先打印 BRANCH 与 STREAM_TYPES 全列表，便于即使全部失败也能据此定位 UI 发布的确切名。
import sys, os, time, multiprocessing as mp

try:
    mp.set_start_method("fork", force=True)
except Exception:
    pass


def _silence():
    # 屏蔽 C 扩展(connect 内部)的 printf 刷屏：重定向真实 fd 1/2 到 /dev/null
    try:
        dn = os.open(os.devnull, os.O_WRONLY)
        os.dup2(dn, 1)
        os.dup2(dn, 2)
    except Exception:
        pass


def worker(client_cls, q, srv, st_name, st_val):
    _silence()
    try:
        c = client_cls(srv, st_val, True)
        # 优先非阻塞 connect（不触发内部 connecting 刷屏）
        try:
            ok = c.connect(False)
        except TypeError:
            ok = c.connect()
        if not ok:
            q.put(("IDLE", srv, st_name))
            return
        buf = c.recv()
        if buf is not None:
            w = getattr(buf, "width", "?")
            h = getattr(buf, "height", "?")
            fmt = getattr(buf, "format", "?")
            q.put(("OK", srv, st_name, w, h, fmt))
        else:
            q.put(("NOFRAME", srv, st_name))
        try:
            c.stop()
        except Exception:
            pass
    except Exception as e:
        q.put(("ERR", srv, st_name, str(e)[:200]))


def probe(client_cls, srv, st_name, st_val, timeout=1.3):
    q = mp.Queue()
    p = mp.Process(target=worker, args=(client_cls, q, srv, st_name, st_val))
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.terminate()
        try:
            p.join(1)
        except Exception:
            pass
        return ("TIMEOUT", srv, st_name)  # stream 类型合法但当前未在发布帧
    try:
        return q.get_nowait() if not q.empty() else ("EMPTY", srv, st_name)
    except Exception:
        return ("EMPTY", srv, st_name)


if __name__ == "__main__":
    sys.path.insert(0, "/data/openpilot")
    try:
        from cereal.visionipc import VisionIpcClient, VisionStreamType
    except Exception:
        from msgq.visionipc import VisionIpcClient, VisionStreamType

    print("PY:", sys.version.split()[0])

    # 分支信息（便于去对应源码查 UI 发布的 server/stream 名）
    branch = "?"
    for p in ["/data/params/d/GitBranch", "/data/openpilot/.git/HEAD"]:
        try:
            b = open(p).read().strip()
            if b:
                branch = b
                break
        except Exception:
            pass
    print("BRANCH:", branch)

    sts = [s for s in dir(VisionStreamType) if not s.startswith("_")]
    print("STREAM_TYPES:", sts)

    SERVERS = ["uiDebug", "ui", ""]
    ui_like = [s for s in sts if "UI" in s]
    found = 0
    print("=== 探测 UI 类 stream（%d 个）===" % len(ui_like))
    for st_name in ui_like:
        st_val = getattr(VisionStreamType, st_name)
        for srv in SERVERS:
            r = probe(VisionIpcClient, srv, st_name, st_val)
            if r[0] == "OK":
                print("CONNECTED srv=%r stream=%s %sx%s fmt=%s" % (r[1], r[2], r[3], r[4], r[5]))
                found += 1
            sys.stdout.flush()

    print("DONE found=%d" % found)
    if found == 0:
        print("未找到可用 UI 屏幕流。请把上方 BRANCH / STREAM_TYPES 列表发我，"
              "我去对应分支源码确认 UI 发布的 (server, stream) 确切名后再适配。")
