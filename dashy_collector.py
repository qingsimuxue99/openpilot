#!/usr/bin/env python3
"""C3 工具箱 - 实时驾驶数据收集器（Dashy 风格，精简只读版）

订阅 openpilot cereal 总线，把车速 / 车道线 / 前车 / 温度等格式化为 JSON，
每 ~100ms 原子写入 /tmp/dashy_state.json，供工具箱前端 Canvas HUD 读取。

设计要点：
- 仅读取 cereal，绝不写入任何参数。
- 用 openpilot 自带的 python 运行（由工具箱探测后拉起），绕开工具箱 venv 的编译 .so ABI 问题。
- cereal 是 openpilot / carrot / sunnypilot / frogpilot 通用的消息总线，五分支都适用。
"""
import sys, os, time, json

OPENPILOT_DIR = "/data/openpilot"
STATE = "/tmp/dashy_state.json"
TMP = "/tmp/dashy_state.tmp.json"
LOG = "/tmp/dashy_state.log"


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


def main():
    if OPENPILOT_DIR not in sys.path:
        sys.path.insert(0, OPENPILOT_DIR)
    from cereal.messaging import SubMaster

    TOPICS = ["carState", "modelV2", "radarState", "liveCalibration",
              "deviceState", "selfdriveState", "liveMapData", "controlsState"]
    sm = SubMaster(TOPICS)

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
