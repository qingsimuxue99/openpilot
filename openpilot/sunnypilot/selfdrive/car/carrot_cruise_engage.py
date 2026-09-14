"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

CP(carrotpilot) 功能移植 —— 自动开启巡航判定：

  1) AutoGasTokSpeed  轻踩油门开启巡航的速度
     巡航未开启时，点一下油门（0.4 s 内抬起）且车速不低于该值 -> 请求开启巡航

  2) CruiseOnDist     定速-自动开启距离
     巡航未开启时，与前车距离小于该值（米）-> 请求开启巡航

本模块负责两件事：
  a) 判定「要不要开巡航」            —— update() / _update_gas_tok() / _lead_distance()
  b) 把判定结果翻译成模拟按下 RES+ 的 CAN 报文 —— create_engage_messages()

两者都在父仓库完成，**opendbc 子模块保持与上游完全一致**，
因此 `git clone --recursive` 直装即可生效，不依赖任何子模块改动。

之所以不改 cereal 的 CarState 结构（CP 是加 activateCruise 字段），
是因为改动 capnp schema 需要重建全部 C++ 目标；而 card.py 的 state_update()
（判定）与 controls_update()（发送）在同一个 step() 内顺序执行、同帧同线程，
所以直接在 card.py 里把报文追加到 can_sends 即可，既不碰 schema 也不影响其他车型。
"""
from openpilot.common.constants import CV
from openpilot.common.params import Params

DT_CTRL = 0.01
GAS_TOK_TIMER = int(0.4 / DT_CTRL)      # 点按油门的时间窗口：0.4 s
GAS_TOK_HOLD = int(0.3 / DT_CTRL)       # 判定成立后的保持时间：0.3 s
PARAM_REFRESH_FRAMES = 10               # 参数刷新周期（100 Hz -> 10 Hz）
MIN_ENGAGE_SPEED_KPH = 10.0             # 无前车时按下巡航键的最低车速（与 CP 一致）
MAX_STEERING_ANGLE_DEG = 70.0           # 方向盘角度过大时不介入（与 CP 一致）


ENGAGE_BUTTON_MIN_INTERVAL_S = 0.5      # 两次模拟按键的最短间隔（与 CP 的按键节奏一致）
ENGAGE_BUTTON_SENDS = 25                # 非 CANFD：连续发 25 帧 CLU11
ENGAGE_BUTTON_SENDS_CANFD = 20          # CANFD：连续发 20 帧 CRUISE_BUTTONS
ENGAGE_BUTTON_BRANDS = ("hyundai", "kia", "genesis")

# 惰性加载：只有真的要发按键时才 import hyundai 的 CAN 构造器，
# 避免非 HKG 车型白白付出 import 开销、也避免 import 失败影响判定逻辑。
_CAN_MODULES = None


def _load_can_modules():
  global _CAN_MODULES
  if _CAN_MODULES is None:
    from opendbc.car.hyundai import hyundaican, hyundaicanfd
    from opendbc.car.hyundai.values import Buttons, HyundaiFlags
    _CAN_MODULES = (hyundaican, hyundaicanfd, Buttons, HyundaiFlags)
  return _CAN_MODULES


class CarrotCruiseEngage:
  def __init__(self, CP, CP_SP):
    self.CP = CP
    self.CP_SP = CP_SP
    self.params = Params()
    self.frame = 0

    self.auto_gas_tok_speed = 0.0   # kph，0 = 关闭
    self.cruise_on_dist = 0.0       # m，0 = 关闭

    self.engage = 0                 # 本帧是否请求开启巡航（1 = 请求）

    self._param_failed = False
    self._gas_frames = 0            # 油门连续按住的帧数
    self._gas_tok = False
    self._gas_tok_left = 0
    self._last_button_frame = 0     # 上次发送模拟按键时的 CarController 帧号

  @property
  def enabled(self) -> bool:
    return self.auto_gas_tok_speed > 0.0 or self.cruise_on_dist > 0.0

  def _refresh_params(self, is_metric: bool) -> None:
    try:
      # CP 侧按当前单位保存：公制 kph，英制 mph -> 统一换算成 kph 比较
      unit_factor = 1.0 if is_metric else CV.MPH_TO_KPH
      # 注意：sunnypilot 的 Params 没有 get_int()（那是 carrotpilot 的扩展），
      # 这里统一用 get(key, return_default=True)，类型由 params_keys.h 决定。
      self.auto_gas_tok_speed = max(0.0, float(self.params.get("AutoGasTokSpeed", return_default=True)) * unit_factor)
      self.cruise_on_dist = max(0.0, float(self.params.get("CruiseOnDist", return_default=True)))
      self._param_failed = False
    except Exception as e:
      # 参数不可用（例如白名单未重新编译）时彻底禁用本功能，绝不影响行车主流程
      self.auto_gas_tok_speed = 0.0
      self.cruise_on_dist = 0.0
      if not self._param_failed:
        print(f"[CarrotCruiseEngage] 读取参数失败，功能已禁用: {type(e).__name__}: {e}")
      self._param_failed = True

  def _update_gas_tok(self, gas_pressed: bool) -> None:
    if gas_pressed:
      self._gas_frames += 1
    elif self._gas_frames > 0:
      # 刚好抬起：只有「短按」才算点油门
      if self._gas_frames <= GAS_TOK_TIMER:
        self._gas_tok_left = GAS_TOK_HOLD
      self._gas_frames = 0

    if self._gas_tok_left > 0:
      self._gas_tok_left -= 1
      self._gas_tok = True
    else:
      self._gas_tok = False

  @staticmethod
  def _lead_distance(sm) -> float:
    try:
      if sm.valid['radarState']:
        lead = sm['radarState'].leadOne
        if lead.status:
          return float(lead.dRel)
    except Exception:
      pass
    return 0.0

  def create_engage_messages(self, cs, cc, cp) -> list:
    """把本帧的开启请求翻译成「模拟按下 RES+」的 CAN 报文。

    这是从 opendbc/car/hyundai/carcontroller.py::create_carrot_engage_messages
    1:1 平移过来的（只把隐式 self 换成显式传入的 cs / cc / cp），
    判据、报文数量、0.5 s 节流、清除时机全部保持原样。
    搬回父仓库是为了让 opendbc 子模块保持与上游一致、仓库可直接安装。

    cs : 平台 CarState（card.py 的 self.CI.CS）—— 提供 out / clu11 / buttons_counter
    cc : CarController 实例（card.py 的 self.CI.CC）—— 提供 packer / CAN / frame
    cp : CarParams —— 提供 brand / flags

    返回要追加到 can_sends 的报文列表（可能为空）。本函数不做任何判定。
    """
    if self.engage <= 0:
      return []

    # 发出去之前再确认一次：有踏板动作、或巡航已经开了，就放弃
    out = getattr(cs, "out", None)
    if out is None or out.gasPressed or out.brakePressed or out.cruiseState.enabled:
      self.engage = 0
      return []

    if cc is None or getattr(cp, "brand", None) not in ENGAGE_BUTTON_BRANDS:
      return []

    # 与 CP 一致：最短 0.5 s 间隔（cc.frame 每个控制周期 +1，即 100 Hz）
    frame = int(getattr(cc, "frame", 0))
    if (frame - self._last_button_frame) * DT_CTRL < ENGAGE_BUTTON_MIN_INTERVAL_S:
      return []

    packer = getattr(cc, "packer", None)
    if packer is None:
      return []

    try:
      hyundaican, hyundaicanfd, Buttons, HyundaiFlags = _load_can_modules()
    except Exception:
      return []

    can_sends = []
    try:
      if cp.flags & HyundaiFlags.CANFD:
        can_bus = getattr(cc, "CAN", None)
        if can_bus is None:
          return []
        counter = int(getattr(cs, "buttons_counter", 0)) + 1
        for _ in range(ENGAGE_BUTTON_SENDS_CANFD):
          can_sends.append(hyundaicanfd.create_buttons(packer, cp, can_bus, counter, Buttons.RES_ACCEL))
      else:
        clu11 = getattr(cs, "clu11", None)
        if clu11 is None:
          return []
        for _ in range(ENGAGE_BUTTON_SENDS):
          can_sends.append(hyundaican.create_clu11(packer, frame, clu11, Buttons.RES_ACCEL, cp))
    except Exception as e:
      if self.frame % 500 == 0:
        print(f"[CarrotCruiseEngage] 生成开启巡航按键报文失败: {type(e).__name__}: {e}")
      return []

    self._last_button_frame = frame
    self.engage = 0
    return can_sends

  def update(self, CS, sm, CC, is_metric: int) -> int:
    """每帧（100 Hz）调用，返回 1 表示请求开启巡航。"""
    self.frame += 1
    if self.frame % PARAM_REFRESH_FRAMES == 0:
      self._refresh_params(is_metric)

    self._update_gas_tok(bool(CS.gasPressed))
    self.engage = 0

    if not self.enabled:
      return 0

    # 前提：原车巡航可用但尚未开启、openpilot 未接管、无踏板 / 大转角干扰
    if not CS.cruiseState.available or CS.cruiseState.enabled or CC.enabled:
      return 0
    if CS.gasPressed or CS.brakePressed:
      return 0
    if CS.vEgo < 0.1 or abs(float(CS.steeringAngleDeg)) > MAX_STEERING_ANGLE_DEG:
      return 0

    v_ego_kph = float(CS.vEgo) * CV.MS_TO_KPH

    # 1) 轻踩油门开启巡航（CP: Cruise on (gas tok)）
    if self.auto_gas_tok_speed > 0.0 and self._gas_tok:
      if v_ego_kph >= self.auto_gas_tok_speed:
        self._gas_tok = False
        self._gas_tok_left = 0
        self.engage = 1
        return 1

    # 2) 定速-自动开启距离（CP: Cruise on (fcw dist)）
    if self.cruise_on_dist > 0.0:
      d_rel = self._lead_distance(sm)
      if 0.0 < d_rel < self.cruise_on_dist:
        if CC.hudControl.leadVisible or v_ego_kph > MIN_ENGAGE_SPEED_KPH:
          self.engage = 1
          return 1

    return 0
