#!/usr/bin/env python3
"""
自动居中纠偏 + 弯道居中（移植自 carrotpilot）

自动居中：检测车道中心偏离，实时修正曲率
弯道居中：弯道时强制沿车道中心，不外扩不内切

参数（存在 Params）：
  AutoCenterMode      0=关 1=实时修正 2=实时+长期学习  默认2
  AutoCenterGain      纠偏强度（×0.01）  默认40 → 0.4
  CurveCenterMode    0=关 1=开  默认0
  CurveCenterGain    居中强度（×0.01）  默认60 → 0.6
  CurveCenterCurv    激活曲率（×0.001） 默认4 → 0.004
"""
import numpy as np
from openpilot.common.params import Params

DT_MDL = 0.01  # 模型周期

class AutoCenter:
  def __init__(self):
    self.params = Params()
    self._pc = 0
    self._mode = 2
    self._gain = 0.4
    self._curve_mode = 0
    self._curve_gain = 0.6
    self._curve_min_curv = 0.004
    self._learned = 0.0
    self._error_filtered = 0.0
    self._applied = 0.0

    # 安全限制
    self.MAX_P_CORRECT = 0.002    # 最大实时修正曲率 (1/m)
    self.MAX_TOTAL = 0.003         # 最大总修正
    self.RATE_LIMIT = 0.0002       # 每秒最大变化

  def _pint(self, key, default):
    """sunnypilot 的 Params 没有 get_int()，用 get(return_default=True) + int 兜底。"""
    try:
      v = self.params.get(key, return_default=True)
      return int(v) if v is not None else default
    except (TypeError, ValueError):
      return default

  def _read_params(self):
    self._pc += 1
    if self._pc % 100 != 0:  # ~1秒读一次
      return
    try:
      self._mode = self._pint("AutoCenterMode", 2)
      g = self._pint("AutoCenterGain", 40)
      self._gain = max(0.1, min(1.0, g * 0.01)) if g > 0 else 0.4

      self._curve_mode = self._pint("CurveCenterMode", 0)
      cg = self._pint("CurveCenterGain", 60)
      self._curve_gain = max(0.1, min(1.0, cg * 0.01)) if cg > 0 else 0.6
      cc = self._pint("CurveCenterCurv", 4)
      self._curve_min_curv = max(0.001, min(0.010, cc * 0.001)) if cc > 0 else 0.004
    except Exception:
      pass

  def update(self, model_v2, v_ego, steering_pressed, lane_change_active=False):
    """
    返回曲率修正量 Δκ
    error > 0 = 车道中心在右边 → 需要右转 → Δκ > 0
    """
    self._read_params()
    target = 0.0

    # —— 闸门条件 ——
    if self._mode <= 0 and self._curve_mode <= 0:
      self._applied = 0.0
      return 0.0
    if steering_pressed:
      self._error_filtered = 0.0
      self._applied = 0.0
      return 0.0
    if lane_change_active:
      self._error_filtered = 0.0
      self._applied = 0.0
      return 0.0
    if v_ego * 3.6 < 15.0:  # 低于15km/h不介入
      self._error_filtered = 0.0
      self._applied = 0.0
      return 0.0

    # 读取车道线
    lll_prob = model_v2.laneLineProbs[1] if len(model_v2.laneLineProbs) > 1 else 0.0
    rll_prob = model_v2.laneLineProbs[2] if len(model_v2.laneLineProbs) > 2 else 0.0
    if lll_prob < 0.5 or rll_prob < 0.5:
      self._error_filtered = 0.0
      self._applied = 0.0
      return 0.0

    lll_y = model_v2.laneLines[1].y[0] if len(model_v2.laneLines[1].y) > 0 else 0.0
    rll_y = model_v2.laneLines[2].y[0] if len(model_v2.laneLines[2].y) > 0 else 0.0

    # error: 车道中心相对车辆的横向偏移（米）
    # y 坐标：正=右，负=左
    error = (lll_y + rll_y) * 0.5  # 车道中心在车右边 = error > 0

    # —— 自动居中 ——
    if self._mode > 0:
      # 一阶滤波
      alpha = 0.1
      self._error_filtered = self._error_filtered * (1 - alpha) + error * alpha

      # P 修正
      p_term = self._error_filtered * self._gain

      # 长期学习（mode>=2）
      if self._mode >= 2:
        # 缓慢学习静态偏差
        self._learned = np.clip(self._learned + self._error_filtered * (DT_MDL / 45.0),
                                -0.3, 0.3)

      target = p_term + self._learned

    # —— 弯道居中 ——
    if self._curve_mode > 0:
      # 当前曲率
      curv = model_v2.action.desiredCurvature if hasattr(model_v2.action, 'desiredCurvature') else 0.0
      if abs(curv) > self._curve_min_curv:
        # 弯道：把 error 朝中心拉
        curve_correct = error * self._curve_gain * 2.0  # 弯道修正更强
        target = target + curve_correct

    # 限制总修正量（转换成曲率）
    # 横向偏移 0.1m ≈ 曲率修正 0.001 (1/m)
    target = np.clip(target * 0.01, -self.MAX_TOTAL, self.MAX_TOTAL)

    # 速率限制
    max_step = self.RATE_LIMIT * DT_MDL
    self._applied = np.clip(target, self._applied - max_step, self._applied + max_step)

    return self._applied
