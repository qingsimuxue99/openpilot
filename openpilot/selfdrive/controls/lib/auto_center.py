#!/usr/bin/env python3
"""
自动居中纠偏 + 弯道居中（移植自 carrotpilot）

2026-09-15 安全性重构 —— 修掉 5 个实质缺陷：

  A 注入点移到 clip_curvature() 之前
    原先 cur_correct 在 clip_curvature() 之后相加 → 完全绕过 ISO 横向 jerk /
    横向加速度 / MAX_CURVATURE 三重限幅，且 curvature_limited 标志不再反映真实曲率。
    现在由调用方把它并入待限幅的目标曲率，安全约束重新生效。

  B 计算改到「横向加速度」域：Δκ = a_lat / v²
    原先 Δκ ∝ error、与车速无关，物理上应为 Δκ ∝ error/v²。
    后果是同样偏差在 100km/h 产生的横向加速度是 30km/h 的约 10 倍（2.2 vs 0.2 m/s²，
    2.2 m/s² 已是紧急变道量级）。现在等效横向加速度与车速无关。

  C 去掉对「总和」的慢速率限制
    原先 RATE_LIMIT=0.0002/s，满量程 0.003 需要 15 秒才建立；而弯道持续通常只有
    5~10 秒，P 项根本来不及到位。现在 P 项立即响应，速率限制交由 clip_curvature
    统一施加（它才是 ISO jerk 的正确施加位置）。本模块仅保留一个宽松兜底限速。

  D 增益改到 a_lat 域并硬上限
    原先 0.3m 的车道内轻微偏差就逼近满量程。现在 P 项上限 0.8 m/s²、含长期学习项
    与弯道项的总上限 1.2 m/s²，语义清晰且不会顶满。

  E 目标一致性保护
    openpilot 横向控制跟的是「模型路径」而不是车道中心。当模型为避障/绕行主动偏移
    时，(路径 - 车道中心) 并不是误差，原实现会当成误差去对抗。现在检测到模型路径
    明显偏离车道中心就完全不介入（fail-safe：宁可不动，不可反向动）。

  F 按横向控制激活状态设闸（复测时发现的连带缺陷）
    原先没有按 CC.latActive 设闸，横向控制未激活时仍在输出修正。注入点移到
    clip_curvature() 之前后这一点更要紧：self.desired_curvature 同时是下一帧速率限制
    的 prev_curvature，被污染会让限幅器状态跑偏。

参数（存在 Params）：
  AutoCenterMode      0=关 1=实时修正 2=实时+长期学习  默认2
  AutoCenterGain      纠偏强度（×0.01）  默认40 → K_a = 0.6 m/s²/m
  CurveCenterMode     0=关 1=开  默认0
  CurveCenterGain     居中强度（×0.01）  默认60
  CurveCenterCurv     激活曲率（×0.001） 默认4 → 0.004 1/m（半径 250m）
"""
import numpy as np
from openpilot.common.params import Params

DT_MDL = 0.01  # 模型/控制周期

# ── 增益与安全上限（全部在横向加速度域，单位 m/s²）──────────────
_A_PER_GAIN = 1.5        # AutoCenterGain(百分数)/100 × 此系数 = K_a [m/s²/m]
_MAX_A_P = 0.8           # P 项单独上限
_MAX_A_TOTAL = 1.2       # 含长期学习项 + 弯道项的总上限
_CURVE_A_SCALE = 2.0     # 弯道项相对自动居中的额外强度
_LEARN_TAU = 45.0        # 长期学习时间常数 (s)
_LEARN_MAX = 0.4         # 长期学习项上限 (m/s²)

# 兜底 Δκ 速率限制 (1/m/s)。正常由 clip_curvature 限速（ISO 横向 jerk），
# 这里只作「万一注入点被挪回限幅之后」的防御，0.02 → 满量程 0.15 s。
_RATE_LIMIT = 0.02

_MIN_V_MS = 15.0 / 3.6   # 介入最低车速 15 km/h
_LANE_PROB_MIN = 0.5

# 前瞻索引：ModelConstants.T_IDXS[10] = 10*(10/32)^2 ≈ 0.98 s（≈19 m）
_PATH_CHK_IDX = 10
_PATH_DIVERGE_M = 1.0    # 模型路径与车道中心偏离超过此值 → 判定为主动 maneuver


class AutoCenter:
  def __init__(self):
    self.params = Params()
    # 置 99 让第一次 update() 就读取真实参数（原先要等 1 秒后才读，
    # 开机首秒用硬编码默认值）
    self._pc = 99
    self._mode = 2
    self._gain = 0.4
    self._curve_mode = 0
    self._curve_gain = 0.6
    self._curve_min_curv = 0.004
    self._learned_a = 0.0          # 长期学习项（横向加速度域）
    self._error_filtered = 0.0
    self._applied = 0.0

  def _pint(self, key, default):
    """sunnypilot 的 Params 没有 get_int()，用 get(return_default=True) + int 兜底。"""
    try:
      v = self.params.get(key, return_default=True)
      return int(v) if v is not None else default
    except (TypeError, ValueError):
      return default

  def _read_params(self):
    self._pc += 1
    if self._pc % 100 != 0:  # ~1 秒读一次
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

  def _reset(self):
    self._error_filtered = 0.0
    self._applied = 0.0

  def _model_is_maneuvering(self, model_v2, lll_y, rll_y) -> bool:
    """E：模型路径是否明显偏离车道中心（= 模型在主动避障/绕行，而非车道保持）。

    position.y 与 laneLines[].y 同属 ModelDataV2 的同一坐标系，取同一前瞻索引比较，
    曲率分量近似抵消，故用绝对差即可，与左右符号约定无关。
    """
    try:
      if len(model_v2.position.y) <= _PATH_CHK_IDX:
        return False
      if len(lll_y) <= _PATH_CHK_IDX or len(rll_y) <= _PATH_CHK_IDX:
        return False
      path_y = float(model_v2.position.y[_PATH_CHK_IDX])
      lane_y = float(lll_y[_PATH_CHK_IDX] + rll_y[_PATH_CHK_IDX]) * 0.5
      return abs(path_y - lane_y) > _PATH_DIVERGE_M
    except (AttributeError, IndexError, TypeError):
      return False

  def update(self, model_v2, v_ego, steering_pressed, lane_change_active=False, lat_active=True):
    """返回曲率修正量 Δκ (1/m)。

    error > 0 = 车道中心在车右边 → 需要右转 → Δκ > 0

    lat_active: 横向控制是否激活。False 时横向执行器没有输出，任何修正都是死代码，
    而且注入点移到 clip_curvature() 之前后会污染 self.desired_curvature（它同时还是
    下一帧速率限制的 prev_curvature），因此必须设闸。
    """
    self._read_params()

    # —— 闸门条件 ——
    if self._mode <= 0 and self._curve_mode <= 0:
      self._reset()
      return 0.0
    if steering_pressed or lane_change_active or not lat_active or v_ego < _MIN_V_MS:
      self._reset()
      return 0.0

    # 读取车道线
    try:
      lll_prob = model_v2.laneLineProbs[1] if len(model_v2.laneLineProbs) > 1 else 0.0
      rll_prob = model_v2.laneLineProbs[2] if len(model_v2.laneLineProbs) > 2 else 0.0
    except (AttributeError, IndexError, TypeError):
      self._reset()
      return 0.0
    if lll_prob < _LANE_PROB_MIN or rll_prob < _LANE_PROB_MIN:
      self._reset()
      return 0.0

    lll_y = model_v2.laneLines[1].y
    rll_y = model_v2.laneLines[2].y
    if len(lll_y) == 0 or len(rll_y) == 0:
      self._reset()
      return 0.0

    # E：模型主动偏移（避障/绕行）时不介入
    if self._model_is_maneuvering(model_v2, lll_y, rll_y):
      self._reset()
      return 0.0

    # error: 车道中心相对车辆的横向偏移（米）
    # laneLines[].y 约定：左为负、右为正 → error > 0 = 车道中心在车右
    error = (float(lll_y[0]) + float(rll_y[0])) * 0.5

    # 一阶滤波（tau ≈ 0.1 s）
    alpha = 0.1
    self._error_filtered = self._error_filtered * (1 - alpha) + error * alpha

    k_a = self._gain * _A_PER_GAIN          # m/s² per m
    a_target = 0.0

    # —— 自动居中（P + 可选 I）——
    if self._mode > 0:
      a_p = float(np.clip(k_a * self._error_filtered, -_MAX_A_P, _MAX_A_P))
      a_target = a_p

      # 长期学习：累积静态偏差，消除稳态残差（mode >= 2）
      if self._mode >= 2:
        self._learned_a = float(np.clip(
          self._learned_a + a_p * (DT_MDL / _LEARN_TAU), -_LEARN_MAX, _LEARN_MAX))
        a_target += self._learned_a

    # —— 弯道居中 ——
    if self._curve_mode > 0:
      curv = float(getattr(model_v2.action, 'desiredCurvature', 0.0))
      if abs(curv) > self._curve_min_curv:
        curve_a = k_a * self._error_filtered * self._curve_gain * _CURVE_A_SCALE
        a_target += float(np.clip(curve_a, -_MAX_A_TOTAL, _MAX_A_TOTAL))

    # 总上限
    a_target = float(np.clip(a_target, -_MAX_A_TOTAL, _MAX_A_TOTAL))

    # 换算曲率：a_lat = Δκ · v²  →  Δκ = a_target / v²
    dk_target = a_target / max(v_ego * v_ego, 1e-3)

    # 兜底速率限制（正常由 clip_curvature 约束）
    max_step = _RATE_LIMIT * DT_MDL
    self._applied = float(np.clip(dk_target, self._applied - max_step, self._applied + max_step))
    return self._applied
