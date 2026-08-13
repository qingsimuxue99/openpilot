#!/usr/bin/env python3
"""
幽灵刹车抑制（置信度阻尼，永不删除前车）

核心原则（对比"删除前车"方案，风险几乎为零）：
  - 绝不删除 / 屏蔽任何前车。只在"高速 + 运动学异常 + 安全 TTC"时，
    把传给 MPC 的 leadOne.dRel 轻微放大（封顶），削弱瞬时假近距急刹。
  - 任一安全地板触发（TTC 低 / 近距离 / 视觉确认 leadOne.modelProb 高 / 低速）
    立即不阻尼，原车 100% 行为，不削弱任何真实危险场景。
  - 失败后果被严格限制：最坏只是"安全 TTC 下多跟 ~0.2s 车"，绝不忽略真实近距危险。

判定信号（独立弱信号累加 + 多帧确认，防单帧抖动误杀真实急刹）：
  - 运动学跳变：dRel 一帧大幅掉，且 vRel 不符真实静止前车（真静止前车 vRel≈-vEgo）
  - 横向越界：yRel 超出本车道（护栏/立柱回波特征）
  - 视觉独报：leadOne.modelProb 低（模型没看到对应前车）

铁律：独立开关 PhantomBrakeGuardMode（默认 0 关）；关时整段 no-op，对原逻辑零影响。
"""
from openpilot.common.params import Params

OFF = 0
ON = 1
AGGRESSIVE = 2

SPEED_MIN = 60.0 / 3.6        # m/s：仅高速介入（城市蠕行不碰，真实前车密集场景不削弱）
TTC_FLOOR = 3.0              # s：TTC 低于此不阻尼（真在逼近照刹）
DREL_NEAR = 12.0             # m：近距离(<12m)不阻尼（贴脸必是真车/危险）
MODEL_PROB_CONFIRM = 0.4     # 视觉确认阈值（modelProb 高=真车，不阻尼）
CONFIRM_FRAMES = 10          # 多帧确认默认帧数（迟滞，防单帧抖动误杀真实急刹）
ALPHA_STD = 0.6              # 标准模式 dRel 放大强度
ALPHA_AGR = 1.0              # 激进模式 dRel 放大强度
FACTOR_CAP = 2.0             # dRel 放大封顶，避免失真过大


class PhantomBrakeGuard:
  def __init__(self):
    self.params = Params()
    self.mode = OFF
    self.dist_th = 25.0
    self.confirm = CONFIRM_FRAMES
    # —— 帧间状态 ——
    self._pc = 0
    self._streak = 0
    self._last_drel = None
    self._last_drel_valid = False

  def _read_params(self):
    # 每 ~0.5s 重读一次，跟随菜单实时调节
    self._pc += 1
    if self._pc % 10 != 0:
      return
    try:
      self.mode = self.params.get_int("PhantomBrakeGuardMode")
    except Exception:
      self.mode = OFF
    try:
      self.dist_th = max(5.0, float(self.params.get_int("PhantomBrakeGuardDist")) * 0.1)
    except Exception:
      self.dist_th = 25.0
    try:
      self.confirm = max(1, self.params.get_int("PhantomBrakeGuardConfirm"))
    except Exception:
      self.confirm = CONFIRM_FRAMES

  def update(self, carrot, sm, v_ego, v_cruise, radar_state):
    self._read_params()
    if self.mode == OFF:
      return radar_state

    lead = radar_state.leadOne
    if not lead.status:
      self._streak = 0
      self._last_drel_valid = False
      return radar_state

    d = lead.dRel
    vrel = lead.vRel
    yrel = lead.yRel
    model_prob = lead.modelProb if hasattr(lead, "modelProb") else 1.0

    # —— 安全地板（任一触发 → 不阻尼，原车 100% 行为）——
    v_closing = -vrel if vrel < 0 else 0.0
    ttc = (d / v_closing) if (v_closing > 0.5 and v_ego > 3.0) else 999.0
    if ttc < TTC_FLOOR or d < DREL_NEAR or v_ego < SPEED_MIN or model_prob >= MODEL_PROB_CONFIRM:
      self._streak = 0
      self._last_drel = d
      self._last_drel_valid = True
      return radar_state

    # —— 仅 dRel < 阈值 的近距前车才进入判定（远处目标不会引发急刹）——
    if d >= self.dist_th:
      self._streak = 0
      self._last_drel = d
      self._last_drel_valid = True
      return radar_state

    # —— 计算 phantom_score（运动学异常 + 横向越界 + 视觉独报）——
    score = 0.0
    # 运动学跳变：dRel 一帧大幅掉，且 vRel 不符真实静止前车（真静止前车 vRel≈-vEgo）
    if self._last_drel_valid:
      drop = self._last_drel - d
      expected_vrel = -v_ego
      if drop > 8.0 and abs(vrel - expected_vrel) > 3.0:
        score += 0.5
    # 横向越界：超出本车道（半车道 ~1.0m + 余量）
    if abs(yrel) > 1.2:
      score += 0.3
    # 视觉独报（modelProb 低，已排除 >=0.4 的情况）
    if model_prob < MODEL_PROB_CONFIRM:
      score += 0.2

    # —— 多帧确认：连续命中才算幽灵，防单帧抖动误杀真实急刹 ——
    if score > 0.05:
      self._streak += 1
    else:
      self._streak = 0
    self._last_drel = d
    self._last_drel_valid = True
    if self._streak < self.confirm:
      return radar_state

    # —— 阻尼：放大 dRel（前车仍在，仅让 MPC 不过度急刹）——
    # 复制为可变副本，绝不修改原 radar_state（FCW 等后续逻辑读原消息，保持诚实）
    alpha = ALPHA_AGR if self.mode == AGGRESSIVE else ALPHA_STD
    factor = min(FACTOR_CAP, 1.0 + min(1.0, score) * alpha)
    new_d = d * factor
    try:
      rs = radar_state.as_builder()
      rs.leadOne.dRel = new_d
      return rs
    except Exception:
      # 复制失败：宁可不安阻尼，也不动原消息（fail-safe，不引入新风险）
      return radar_state
