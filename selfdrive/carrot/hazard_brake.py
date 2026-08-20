#!/usr/bin/env python3
"""
急刹自动双闪 Hazard Brake（独立模块，不修改原纵向/横向控制代码）

功能
  检测到急减速（或 FCW 碰撞预警）时自动点亮车辆双闪，警示后方来车降低追尾风险；
  减速恢复后延时熄灭；急刹停稳后保持一段时间再熄灭（等效 ESS 紧急制动警示灯）。

设计原则（同 phantom_brake_guard / launch_assist）
  - 独立开关，默认 0=关；mode==0 时整段 no-op，对原逻辑零影响。
  - 只读 carState（aEgo/vEgo/gearShifter）与 longitudinalPlan.fcw / radarState.leadOne.fcw，
    不写任何控制量；双闪通过 CC.leftBlinker + CC.rightBlinker 同时置位下发：
    Hyundai CANFD 车型在 create_spas_messages 中映射为 SPAS2 BLINKER_CONTROL=1(hazards)。
  - 迟滞 + 多帧确认，防颠簸/单帧噪声误触发。

参数（均已注册 params_keys.h, PERSISTENT）
  HazardBrakeMode      0:关 1:急刹触发 2:急刹+FCW触发
  HazardBrakeAccel     触发减速度 x0.1 m/s^2, 默认-35(-3.5)
  HazardBrakeRelease   释放减速度 x0.1 m/s^2, 默认-15(-1.5, 迟滞下限)
  HazardBrakeMinSpeed  最低触发车速 km/h, 默认30
  HazardBrakeHold      熄灭延时 x0.1s, 默认15(1.5s)
  HazardBrakeConfirm   触发确认 x0.1s, 默认3(0.3s)
"""

from cereal import car
from openpilot.common.params import Params

GearShifter = car.CarState.GearShifter

OFF = 0

PARAM_RELOAD_SEC = 0.5    # 参数热加载周期（改设置无需重启）
V_STANDSTILL = 0.5        # m/s，低于此视为停稳
STANDSTILL_OFF_SEC = 5.0  # 急刹停稳后双闪保持时长


class HazardBrake:

  def __init__(self):
    self.params = Params()
    # 状态
    self.active = False          # 当前双闪是否点亮
    self.trig_time = 0.0         # 连续满足触发条件的时长(s)
    self.rel_time = 0.0          # 触发条件消失后的时长(s)
    self.standstill_time = 0.0   # 停稳持续时长(s)
    # 参数缓存（含默认值）
    self._reload = 0.0
    self.mode = OFF
    self.accel_trig = -3.5
    self.accel_rel = -1.5
    self.min_speed = 30.0
    self.hold_sec = 1.5
    self.confirm_sec = 0.3

  def _load_params(self):
    try:
      self.mode = self.params.get_int("HazardBrakeMode")
      v = self.params.get_int("HazardBrakeAccel")
      if v != 0:
        self.accel_trig = v * 0.1
      v = self.params.get_int("HazardBrakeRelease")
      if v != 0:
        self.accel_rel = -abs(v) * 0.1
      v = self.params.get_int("HazardBrakeMinSpeed")
      if v > 0:
        self.min_speed = float(v)
      v = self.params.get_int("HazardBrakeHold")
      if v > 0:
        self.hold_sec = v * 0.1
      v = self.params.get_int("HazardBrakeConfirm")
      if v > 0:
        self.confirm_sec = v * 0.1
    except Exception:
      self.mode = OFF   # 读参异常时安全回退为关闭

  def reset(self):
    self.active = False
    self.trig_time = 0.0
    self.rel_time = 0.0
    self.standstill_time = 0.0

  def update(self, CS, long_plan, radar_state=None, dt=0.01):
    """controlsd 每周期(100Hz)调用; 返回 True 表示应点亮双闪"""
    # 参数热加载
    self._reload += dt
    if self._reload >= PARAM_RELOAD_SEC:
      self._reload = 0.0
      self._load_params()

    if self.mode == OFF:
      if self.active:
        self.reset()
      return False

    # 安全闸门：倒挡不触发
    if CS.gearShifter == GearShifter.reverse:
      self.reset()
      return False

    v_ego = CS.vEgo
    a_ego = CS.aEgo

    # 触发条件
    hard = (a_ego <= self.accel_trig) and (v_ego * 3.6 >= self.min_speed)
    fcw = False
    if self.mode >= 2:
      fcw = bool(long_plan.fcw) if long_plan is not None else False
      if not fcw and radar_state is not None:
        fcw = bool(radar_state.leadOne.fcw)
    trig_now = hard or fcw

    if trig_now:
      self.trig_time += dt
      self.rel_time = 0.0
    else:
      self.trig_time = 0.0
      self.rel_time += dt

    # 停稳计时
    if v_ego < V_STANDSTILL:
      self.standstill_time += dt
    else:
      self.standstill_time = 0.0

    if not self.active:
      # 多帧确认后点亮
      if self.trig_time >= self.confirm_sec:
        self.active = True
    else:
      if v_ego < V_STANDSTILL:
        # 急刹停稳: 保持双闪警示后车, 超时后熄灭
        if self.standstill_time >= STANDSTILL_OFF_SEC:
          self.reset()
      else:
        # 行进中: 减速恢复到释放阈值以上并保持 hold_sec 后熄灭(迟滞防抖)
        if (a_ego > self.accel_rel) and (self.rel_time >= self.hold_sec):
          self.reset()

    return self.active
