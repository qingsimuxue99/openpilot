#!/usr/bin/env python3
"""
入弯预备减速（独立模块，不修改原纵向控制代码）

功能：接近弯道时，利用前方路径曲率剖面，提前（比原车弯速逻辑更早）把目标巡航速度
柔和降到弯道限速，使减速分散在更长距离上，避免"弯中急刹"的突兀感。

设计原则（同 launch_assist / traffic_light_brake）：
  - 独立开关 CurveAnticipateMode，默认 0（关）。关闭时整段 no-op，对原逻辑零影响。
  - 只读取 lateralPlan 曲率剖面与 carrotMan.vTurnSpeed，在 carrot.update() 之后、
    mpc.update() 之前微调 carrot.v_cruise（下限再夹一层 carrot 自身弯道限速，绝不突破）。
  - 安全闸门：用户踩油门/刹车时不干预（弯道需要转向，故不按转向角拦截）。
  - 多帧确认：连续若干帧确认前方确有更慢弯道才介入，抑制单帧曲率噪声误触发。

参数：
  CurveAnticipateMode   0/1 总开关
  CurveAnticipateDist   预备前瞻距离(×1m) 默认60，范围20-150：往前看多远开始预备降速
  CurveAnticipateLatA   预备横向加速度(×0.1m/s^2) 默认28 => 2.8：由曲率估算弯道限速时用的
                       目标横向加速度，越大越晚/越少降，越小越早/越多降（下限由 carrot 弯道
                       限速兜底，不会过慢）
"""
import numpy as np
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL

OFF = 0
ON = 1

MIN_SPEED_ANTICIPATE = 1.5   # m/s：低于此速（基本停车）不预备
CONFIRM_FRAMES = 3           # 连续确认帧数（~0.15s @20Hz），抗曲率噪声


class CurveAnticipate:
  def __init__(self):
    self.params = Params()
    self.mode = OFF
    self.lookahead = 60.0     # m
    self.a_lat = 2.8          # m/s^2
    self.streak = 0
    self._pc = 0

  def _read_params(self):
    # 每 ~0.5s 重读一次，跟随菜单实时调节
    self._pc += 1
    if self._pc % 10 != 0:
      return
    try:
      self.mode = self.params.get_int("CurveAnticipateMode")
      self.lookahead = max(20.0, float(self.params.get_int("CurveAnticipateDist")))
      self.a_lat = max(1.0, float(self.params.get_int("CurveAnticipateLatA")) * 0.1)
    except Exception:
      # 参数未编译进 params_keys.h 前（理论上不会发生）一律当作关闭，零影响
      self.mode = OFF

  def _reset(self):
    self.streak = 0

  def update(self, carrot, sm, v_ego, v_cruise):
    self._read_params()
    if self.mode == OFF:
      self._reset()
      return

    cs = sm['carState']
    # 用户主动加减速：完全不干预，原逻辑主导
    if cs.gasPressed or cs.brakePressed:
      self._reset()
      return
    # 基本停车时不预备
    if v_ego < MIN_SPEED_ANTICIPATE:
      self._reset()
      return

    # —— 前方弯道限速（由路径曲率剖面估算，取前瞻窗口内的最小值）——
    v_anti = 1e9
    try:
      lat = sm['lateralPlan']
      curv = np.array(lat.curvatures, dtype=float)
      dist = np.array(lat.distances, dtype=float)
      if len(curv) > 1 and len(dist) == len(curv):
        for i in range(len(curv)):
          d = dist[i]
          if d <= 0.0 or d > self.lookahead:
            continue
          c = abs(curv[i])
          if c > 1e-4:
            v_lim = np.sqrt(self.a_lat / c)
            if v_lim < v_anti:
              v_anti = v_lim
    except Exception:
      v_anti = 1e9

    # 前方没有更慢弯道：不介入
    if v_anti >= v_cruise - 0.3:
      self.streak = 0
      return

    # 多帧确认：确为真实弯道而非单帧曲率噪声，才施加预备降速
    self.streak += 1
    if self.streak < CONFIRM_FRAMES:
      return

    # 目标 = 当前目标与预备限速的较小值；再夹下限 = carrot 自身弯道限速，绝不突破原车意图
    target = min(v_cruise, v_anti)
    try:
      vt = abs(sm['carrotMan'].vTurnSpeed) / 3.6  # km/h -> m/s
      if 0.0 < vt < 199.0 / 3.6:
        target = max(target, vt)
    except Exception:
      pass

    if target < carrot.v_cruise:
      carrot.v_cruise = target
