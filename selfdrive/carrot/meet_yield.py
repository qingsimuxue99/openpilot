#!/usr/bin/env python3
"""
窄路会车让行 Meet Yield（独立模块，不修改原横向控制代码）

功能
  乡道/窄路对向来车时，自动向路肩一侧平移行驶路径让出会车空间，对向车通过后自动回正。
  使用原车毫米波雷达 leadLeft 识别对向车：
    对向车接近速率 vRel = v_target - v_ego，会车场景为强负值
    （例：本车 40km/h + 对向 40km/h => vRel ≈ -22 m/s；同向慢车一般 > -8 m/s）。

架构（两层，照搬 CurveCentering 独立模块模式）
  1) MeetYieldDetector  跑在 carrot_serv 主循环(20Hz)：对向车判定状态机，
     结果 0/1/2 经 carrotMan.meetYieldState 发布（capnp 运行时加载, 无需编译）。
  2) MeetYieldPath      跑在 lateral_planner(plannerd, 20Hz)：读 meetYieldState，
     对 path_xyz[:,1] 做限幅+限速的平移注入；右侧空间不足时自动压缩让行幅度。

设计原则
  - 开关默认 0=关；mode==0 时整段 no-op，对原逻辑零影响。
  - 让行方向与设置页 PathOffset 同语义（+ = 向右），方向不符可设 MeetYieldDir=-1 反转。
  - 斜率限制缓入缓出，不产生突兀转向。

参数（均注册 params_keys.h, PERSISTENT）
  MeetYieldMode     0:关 1:开
  MeetYieldOffset   最大让行幅度 x0.01m, 默认35(=0.35m)
  MeetYieldDist     触发距离 x1m, 默认60(m)
  MeetYieldVRel     接近速率阈值 x0.1m/s, 默认80(=8.0m/s)
  MeetYieldConfirm  触发确认 x0.1s, 默认3(=0.3s)
  MeetYieldRate     平移速率 x0.01m/s, 默认10(=0.10m/s)
  MeetYieldDir      让行方向, 默认1(+1=向右/路肩, -1=向左)
"""

import numpy as np
from openpilot.common.params import Params

OFF = 0

# ---- 状态值（发布到 carrotMan.meetYieldState）----
ST_IDLE = 0      # 无对向车
ST_YIELD = 1     # 让行中（目标车道偏移）
ST_RELEASE = 2   # 对向车已通过, 回正中

PARAM_RELOAD_SEC = 0.5   # 参数热加载周期
MISS_TOLERANCE_SEC = 0.5 # 让行中雷达单帧丢失容忍时长（防遮挡抖动）
RELEASE_SEC = 1.5        # 回正状态保持时长（此后回 IDLE）
DONE_DREL_MARGIN = 15000 # 目标 drel 比触发距离还远 15m 视为已通过(mm)
VREL_DONE = -2.0         # 接近速率回升到此值以上 => 已错过/同向(m/s)

# ---- 注入层闸门常量 ----
MIN_SPEED_KMH = 15.0     # 低于此速度不介入（低速会车人工控制）
MAX_SPEED_KMH = 90.0     # 高于此速度不介入（高速无对向车道场景）
EDGE_MARGIN = 0.30       # m：距右侧车道线/感知边界的最小保留余量
NO_LINE_SCALE = 0.6      # 无右车道线置信度时的幅度折减（窄路常无标线）


class MeetYieldDetector:
  """对向车检测状态机（carrot_serv 主循环调用, 20Hz）"""

  def __init__(self):
    self.params = Params()
    self.state = ST_IDLE
    self.trig_time = 0.0
    self.rel_time = 0.0
    self.miss_time = 0.0
    self._reload = 0.0
    self.mode = OFF
    self.dist_trig = 60.0      # m
    self.vrel_trig = -8.0      # m/s
    self.confirm_sec = 0.3
    self._load_params()

  def _load_params(self):
    try:
      self.mode = self.params.get_int("MeetYieldMode")
      v = self.params.get_int("MeetYieldDist")
      if v > 0:
        self.dist_trig = float(v)
      v = self.params.get_int("MeetYieldVRel")
      if v > 0:
        self.vrel_trig = -abs(v) * 0.1
      v = self.params.get_int("MeetYieldConfirm")
      if v > 0:
        self.confirm_sec = v * 0.1
    except Exception:
      self.mode = OFF

  def reset(self):
    self.state = ST_IDLE
    self.trig_time = 0.0
    self.rel_time = 0.0
    self.miss_time = 0.0

  def update(self, lf_drel_mm, lf_vrel_ms, dt=0.05):
    """lf_drel_mm: 原车雷达 leadLeft.dRel*1000(mm); lf_vrel_ms: vRel(m/s)
       返回状态 0/1/2"""
    self._reload += dt
    if self._reload >= PARAM_RELOAD_SEC:
      self._reload = 0.0
      self._load_params()

    if self.mode == OFF:
      if self.state != ST_IDLE:
        self.reset()
      return ST_IDLE

    seen = (lf_drel_mm is not None) and (lf_vrel_ms is not None)
    closing = seen and (lf_drel_mm < self.dist_trig * 1000.0) and (lf_vrel_ms < self.vrel_trig)

    if self.state == ST_IDLE:
      if closing:
        self.trig_time += dt
        if self.trig_time >= self.confirm_sec:
          self.state = ST_YIELD
          self.rel_time = 0.0
          self.miss_time = 0.0
      else:
        self.trig_time = 0.0

    elif self.state == ST_YIELD:
      done = False
      if not seen:
        # 单帧丢失容忍（对向车近处可能被遮挡/雷达跳变）
        self.miss_time += dt
        if self.miss_time > MISS_TOLERANCE_SEC:
          done = True
      else:
        self.miss_time = 0.0
        # 已通过：距离重新拉开 或 接近速率消失
        if lf_drel_mm > (self.dist_trig + DONE_DREL_MARGIN / 1000.0) * 1000.0 or lf_vrel_ms > VREL_DONE:
          done = True
      if done:
        self.state = ST_RELEASE
        self.rel_time = 0.0

    elif self.state == ST_RELEASE:
      self.rel_time += dt
      # 回正途中对向车再次接近 => 立即重新让行
      if closing:
        self.state = ST_YIELD
        self.trig_time = 0.0
      elif self.rel_time >= RELEASE_SEC:
        self.reset()

    return self.state


class MeetYieldPath:
  """路径平移注入（lateral_planner 调用, 20Hz, 照搬 CurveCentering 模式）"""

  def __init__(self):
    self.params = Params()
    self._pc = 0
    self.mode = OFF
    self.offset_max = 0.35     # m
    self.rate = 0.10           # m/s
    self.direction = 1         # +1=向右(路肩)
    self.applied = 0.0         # 当前已注入偏移(m)
    self.active = False
    self.debug = ""

  def _read_params(self):
    self._pc += 1
    if self._pc % 10 != 0:
      return
    try:
      self.mode = self.params.get_int("MeetYieldMode")
      v = self.params.get_int("MeetYieldOffset")
      if v > 0:
        self.offset_max = min(0.60, max(0.10, v * 0.01))
      v = self.params.get_int("MeetYieldRate")
      if v > 0:
        self.rate = min(0.30, max(0.03, v * 0.01))
      d = self.params.get_int("MeetYieldDir")
      if d != 0:
        self.direction = 1 if d > 0 else -1
    except Exception:
      self.mode = OFF

  def update(self, carrot, sm, path_xyz, LP, v_ego, CS, dt=0.05):
    self._read_params()
    self.active = False
    self.debug = ""

    if self.mode == OFF:
      self.applied = 0.0
      return path_xyz

    # 检测状态（detector 在 carrot_serv, 经 carrotMan 传来）
    try:
      my_state = int(sm['carrotMan'].meetYieldState)
    except Exception:
      my_state = ST_IDLE

    # —— 生效闸门 ——
    if my_state == ST_IDLE:
      target = 0.0
    else:
      target = self.offset_max

      # 速度窗：会车场景为低中速窄路
      v_kmh = v_ego * 3.6
      if v_kmh < MIN_SPEED_KMH or v_kmh > MAX_SPEED_KMH:
        target = 0.0
        self.debug = "速度窗外"
      # 变道中不介入（与 ATC/手动变道互斥, 防叠加）
      elif getattr(LP, "lane_change_multiplier", 1.0) < 0.5:
        target = 0.0
        self.debug = "变道中"
      # 驾驶员打方向让权
      elif getattr(CS, "steeringPressed", False):
        target = 0.0
        self.debug = "人工转向"

      if target > 0.0:
        # 右侧空间约束：不压右车道线（保留 EDGE_MARGIN 余量）
        rll_prob = getattr(LP, "rll_prob", 0.0)
        rll = getattr(LP, "rll_y", None)
        if rll_prob > 0.5 and rll is not None and len(rll) > 0:
          room = float(rll[0]) - EDGE_MARGIN
          if room < self.offset_max:
            target = max(0.0, room)
            self.debug = f"右侧限{target*100:.0f}cm"
        else:
          # 无右线置信度（窄路常见）：幅度折减, 保守让行
          target *= NO_LINE_SCALE
          self.debug = "无右线保守"

    # —— 斜率限制缓入缓出 ——
    max_step = self.rate * dt
    if target > self.applied:
      self.applied = min(target, self.applied + max_step)
    else:
      self.applied = max(target, self.applied - max_step)

    if abs(self.applied) > 0.005:
      self.active = True
      # 方向: 与设置页 PathOffset 同语义(+ = 向右)
      path_xyz[:, 1] = path_xyz[:, 1] + self.direction * self.applied
    return path_xyz
