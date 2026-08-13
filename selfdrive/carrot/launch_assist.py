#!/usr/bin/env python3
"""
起步与跟车辅助（独立模块，不修改原纵向控制代码）

三个独立功能，各自独立开关；任何一个关闭时对原逻辑零影响：
  1) LaunchSmoothingMode  平顺起步：从停止起步时限制初始加速，避免窜动（像人缓给油，不"窜"）
  2) TrafficJamCreepMode   低速拥堵蠕行：低速跟车小幅前挪，减少被加塞、更跟手
  3) LeadDepartureMode     前车起步预判：前车一动立即更跟手起步（与平顺起步互斥，单独响应）

设计原则（同 phantom_brake_guard）：
  - 所有开关默认 0（关）。某功能 mode==0 时该段整段 no-op。
  - 完全复用原 carrot 输出的字段（carrot.v_cruise/stop_dist/mode）与 radar lead，
    在其之上做微调，不改原文件。
  - 安全闸门：用户踩油门/刹车、大幅转向时一律不干预。

参数：
  LaunchSmoothingMode   0/1 平顺起步开关
  LaunchSmoothingInit   初始缓给速度(×0.1m/s) 默认15 => 1.5m/s（起步窗口内目标不超过 v_ego+该值）
  TrafficJamCreepMode   0/1 拥堵蠕行开关
  TrafficJamCreepSpeed  蠕行速度(×0.1m/s) 默认10 => 1.0m/s
  LeadDepartureMode     0/1 前车起步预判开关
  LeadDepartureSpeed    跟车起步目标速度(×0.1m/s) 默认40 => 4.0m/s（前车一动时我们的目标上限）
"""
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.carrot.carrot_functions import TrafficState, XState

OFF = 0
ON = 1

LAUNCH_WINDOW_V = 2.0      # m/s：低于此速视为"起步窗口"，平顺起步在此窗口内生效
LEAD_DEPART_VREL = 0.6     # m/s：前车速度从低于此值升到高于此值视为"起步"


class LaunchAssist:
  def __init__(self):
    self.params = Params()
    # —— 平顺起步 (#1) ——
    self.launch_mode = OFF
    self.launch_init = 1.5
    self.prev_standstill = True
    self.prev_v_ego = 0.0
    self.launch_counter = 0
    # —— 拥堵蠕行 (#5) ——
    self.creep_mode = OFF
    self.creep_speed = 1.0
    # —— 前车起步预判 (#7) ——
    self.depart_mode = OFF
    self.depart_speed_cap = 4.0
    self.lead_v_prev = 0.0
    self.depart_streak = 0
    # —— 参数重读节流 ——
    self._pc = 0

  def _read_params(self):
    # 每 ~0.5s 重读一次，跟随菜单实时调节
    self._pc += 1
    if self._pc % 10 != 0:
      return
    try:
      self.launch_mode = self.params.get_int("LaunchSmoothingMode")
      self.launch_init = max(0.3, float(self.params.get_int("LaunchSmoothingInit")) * 0.1)
      self.creep_mode = self.params.get_int("TrafficJamCreepMode")
      self.creep_speed = max(0.3, float(self.params.get_int("TrafficJamCreepSpeed")) * 0.1)
      self.depart_mode = self.params.get_int("LeadDepartureMode")
      self.depart_speed_cap = max(1.0, float(self.params.get_int("LeadDepartureSpeed")) * 0.1)
    except Exception:
      # 未编译进 params_keys.h 前（理论上不会发生）一律当作全部关闭，零影响
      self.launch_mode = OFF
      self.creep_mode = OFF
      self.depart_mode = OFF

  def _reset(self):
    self.prev_standstill = True
    self.prev_v_ego = 0.0
    self.launch_counter = 0
    self.lead_v_prev = 0.0
    self.depart_streak = 0

  def update(self, carrot, sm, v_ego, v_cruise):
    self._read_params()
    if self.launch_mode == OFF and self.creep_mode == OFF and self.depart_mode == OFF:
      self._reset()
      return

    cs = sm['carState']
    # 用户主动控速 / 大幅转向：完全不干预，原逻辑主导
    if cs.gasPressed or cs.brakePressed or abs(cs.steeringAngleDeg) > 20:
      self._reset()
      return

    lead = sm['radarState'].leadOne
    lead_status = lead.status
    lead_v = lead.vLead if lead_status else 0.0
    lead_d = lead.dRel if lead_status else 1e9

    # ===== #7 前车起步预判（优先级最高，覆盖平顺起步）=====
    depart_active = False
    if self.depart_mode != OFF and lead_status and lead_d < 50.0 and v_ego < 2.0:
      # 前车速度从静止/极慢升到可行驶 => 起步
      if self.lead_v_prev < LEAD_DEPART_VREL and lead_v > LEAD_DEPART_VREL:
        self.depart_streak += 1
      else:
        self.depart_streak = max(0, self.depart_streak - 1)
      if self.depart_streak >= 2:
        depart_active = True
    else:
      self.depart_streak = 0

    if depart_active:
      # 立即更跟手：把目标抬到前车速度上限，避免等前车动了才慢慢反应
      target = min(max(lead_v, v_cruise), self.depart_speed_cap)
      if carrot.v_cruise < target:
        carrot.v_cruise = target
      # 若原逻辑还停在 e2eStop，强制切到巡航起步
      if carrot.xState in (XState.e2eStop, XState.e2eStopped) and not cs.leftBlinker:
        carrot.xState = XState.e2eCruise
        carrot.stop_dist = 0.0
        carrot.mode = 'acc'

    # ===== #1 平顺起步（前车起步预判激活时跳过，避免互相打架）=====
    launching = (v_ego < LAUNCH_WINDOW_V) and (carrot.v_cruise > 0.5) and (carrot.trafficState != TrafficState.red)
    if self.launch_mode != OFF and launching and not depart_active:
      # 起步窗口内，把目标限制在"当前速度+缓给量"，MPC 只小幅加速 => 不窜
      cap = v_ego + self.launch_init
      if carrot.v_cruise > cap:
        carrot.v_cruise = cap
      self.launch_counter += 1
    else:
      self.launch_counter = 0

    # ===== #5 低速拥堵蠕行（红灯时不蠕行，避免过线）=====
    if (self.creep_mode != OFF and v_ego < 0.8 and lead_status and lead_d < 30.0
        and lead_v < 1.5 and carrot.trafficState != TrafficState.red):
      # 停/极慢且与前车很近：小幅前挪缩小车距，减少被加塞、更跟手
      creep_target = min(self.creep_speed, lead_v + 0.3)
      if carrot.v_cruise < creep_target:
        carrot.v_cruise = creep_target

    # —— 帧间状态 ——
    self.prev_standstill = cs.standstill
    self.prev_v_ego = v_ego
    self.lead_v_prev = lead_v
