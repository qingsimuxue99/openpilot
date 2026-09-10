#!/usr/bin/env python3
"""
弯道居中（独立模块，不修改原横向控制代码）

功能：弯道中强制沿"模型车道线中心"行驶，不外扩、不内切、避免压线。
  - 仅读取模型输出的车道线（lll_y / rll_y）实时计算车道几何中心；
  - 在弯道（曲率超过阈值）时，把期望横向路径朝车道中心回正（强度可调）；
  - 保留用户的 PathOffset / 自动纠偏等意图偏移，只在弯道里修正"偏离分量"；
  - 每点都夹在车道线内侧一定余量内，确保不压线。

设计原则：
  - 开关默认 0（关）。mode==0 时整段 no-op，对原逻辑零影响。
  - 仅在车道线置信度高、未打方向、车速>15km/h、且处于弯道时生效；
    其余情况原样返回，不影响直道与正常跟线。
  - 完全复用原 carrot / LanePlanner 已有字段，不改动原文件。

参数：
  CurveCenteringMode      0/1 弯道居中开关
  CurveCenteringStrength  居中强度(×0.01) 默认60 => 0.6（朝中心回正比例，越大越狠）
  CurveCenteringCurv      激活曲率(×0.001 1/m) 默认4 => 0.004（仅曲率大于此值才生效）
"""
import numpy as np
from openpilot.common.params import Params

OFF = 0
ON = 1

MARGIN = 0.15          # m：保持距车道线内侧的余量，防压线
MIN_SPEED_KMH = 15.0   # 低于此速度不介入（弯道一般在车速较高时出现）
CAMERA_OFFSET = 0.0    # 与本 fork 的 lane_planner_2.CAMERA_OFFSET 保持一致（此处为 0）


class CurveCentering:
  def __init__(self):
    self.params = Params()
    self._mode = OFF
    self._strength = 0.6
    self._min_curv = 0.004
    self._pc = 0
    self.active = False
    self.applied = 0.0  # 最近一帧平均修正量(m)，用于调试

  def _read_params(self):
    self._pc += 1
    if self._pc % 10 != 0:
      return
    try:
      self._mode = self.params.get_int("CurveCenteringMode")
      s = self.params.get_int("CurveCenteringStrength")
      self._strength = max(0.1, min(1.0, s * 0.01)) if s > 0 else 0.6
      c = self.params.get_int("CurveCenteringCurv")
      self._min_curv = max(0.001, min(0.010, c * 0.001)) if c > 0 else 0.004
    except Exception:
      self._mode = OFF

  def update(self, carrot, sm, path_xyz, LP, curvature, v_ego, CS):
    self._read_params()
    self.active = False
    self.applied = 0.0
    if self._mode == OFF:
      return path_xyz

    # —— 安全/生效闸门 ——
    lll_prob = getattr(LP, "lll_prob", 0.0)
    rll_prob = getattr(LP, "rll_prob", 0.0)
    if not (lll_prob > 0.5 and rll_prob > 0.5):
      return path_xyz
    if getattr(LP, "lane_change_multiplier", 1.0) < 0.5:  # 变道中不管
      return path_xyz
    if getattr(CS, "steeringPressed", False):
      return path_xyz
    if v_ego * 3.6 < MIN_SPEED_KMH:
      return path_xyz
    if abs(curvature) < self._min_curv:  # 仅弯道
      return path_xyz

    lll = getattr(LP, "lll_y", None)
    rll = getattr(LP, "rll_y", None)
    ll_x = getattr(LP, "ll_x", None)
    if lll is None or rll is None or ll_x is None:
      return path_xyz
    try:
      lll = np.asarray(lll, dtype=float)
      rll = np.asarray(rll, dtype=float)
      ll_x = np.asarray(ll_x, dtype=float)
      x = np.asarray(path_xyz[:, 0], dtype=float)
      center = (lll + rll) * 0.5
      center_x = np.interp(x, ll_x, center)
    except Exception:
      return path_xyz

    # 当前期望路径（已含用户偏移与自动纠偏）
    y = np.asarray(path_xyz[:, 1], dtype=float)
    # 还原"纯跟线分量"，仅修正弯道里的偏离，不动用户/自动纠偏意图
    off = CAMERA_OFFSET
    lof = getattr(LP, "lane_offset_filtered", None)
    if lof is not None:
      off += float(lof.x)
    off += float(getattr(LP, "ac_applied", 0.0))
    pure = y - off

    # 朝车道中心回正 k 比例（不外扩/不内切）
    k = self._strength
    new_pure = pure * (1.0 - k) + center_x * k

    # 夹在车道线内侧，避免压线
    half = (rll - lll) * 0.5
    lim = np.clip(half - MARGIN, 0.05, 5.0)
    new_pure = np.clip(new_pure, center_x - lim, center_x + lim)

    # 还原用户/自动纠偏偏移
    new_y = new_pure + off
    self.applied = float(np.mean(new_y - y))
    self.active = True
    path_xyz[:, 1] = new_y
    return path_xyz
