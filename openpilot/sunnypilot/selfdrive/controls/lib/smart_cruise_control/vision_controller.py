"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

import openpilot.cereal.messaging as messaging
from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.constants import CV
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.selfdrive.controls.lib.smart_cruise_control import MIN_V

VisionState = custom.LongitudinalPlanSP.SmartCruiseControl.VisionState

ACTIVE_STATES = (VisionState.entering, VisionState.turning, VisionState.leaving)
ENABLED_STATES = (VisionState.enabled, VisionState.overriding, *ACTIVE_STATES)

_ENTERING_PRED_LAT_ACC_TH = 1.3  # Predicted Lat Acc threshold to trigger entering turn state.
_ABORT_ENTERING_PRED_LAT_ACC_TH = 1.1  # Predicted Lat Acc threshold to abort entering state if speed drops.

_TURNING_LAT_ACC_TH = 1.6  # Lat Acc threshold to trigger turning state.

_LEAVING_LAT_ACC_TH = 1.3  # Lat Acc threshold to trigger leaving turn state.
_FINISH_LAT_ACC_TH = 1.1  # Lat Acc threshold to trigger the end of the turn cycle.

_A_LAT_REG_MAX = 2.  # Maximum lateral acceleration

# CP 移植：功能菜单可调项。默认值严格等于上游行为（100% / 0 km/h / 关闭），
# 因此不动参数时这套增强完全不改变车辆表现。
_A_LAT_REG_MAX_DEFAULT = _A_LAT_REG_MAX
_AGGRESSIVENESS_MIN = 50
_AGGRESSIVENESS_MAX = 150
# 保底速度的物理安全上限（m/s²）。弯道太急时 safe_v 会低于保底值，保底自动失效，
# 仍由物理决定，因此这里不可能让车辆以超出横向极限的速度过弯。
_CURVE_FLOOR_MAX_LAT_ACC = 2.8


def _read_int_param(params, key: str, default: int, lo: int, hi: int) -> int:
  """CP 移植：sunnypilot 的 Params 没有 get_int()，用 get(return_default=True) 兜底。"""
  try:
    v = params.get(key, return_default=True)
    v = int(v) if v is not None else default
  except (TypeError, ValueError):
    v = default
  return max(lo, min(hi, v))

_NO_OVERSHOOT_TIME_HORIZON = 4.  # s. Time to use for velocity desired based on a_target when not overshooting.

# Lookup table for the minimum smooth deceleration during the ENTERING state
# depending on the actual maximum absolute lateral acceleration predicted on the turn ahead.
_ENTERING_SMOOTH_DECEL_V = [-0.2, -1.]  # min decel value allowed on ENTERING state
_ENTERING_SMOOTH_DECEL_BP = [1.3, 3.]  # absolute value of lat acc ahead

# Lookup table for the acceleration for the TURNING state
# depending on the current lateral acceleration of the vehicle.
_TURNING_ACC_V = [0.5, 0., -0.4]  # acc value
_TURNING_ACC_BP = [1.5, 2.3, 3.]  # absolute value of current lat acc

_LEAVING_ACC = 0.5  # Conformable acceleration to regain speed while leaving a turn.


class SmartCruiseControlVision:
  v_target: float = 0
  a_target: float = 0.
  v_ego: float = 0.
  a_ego: float = 0.
  output_v_target: float = V_CRUISE_UNSET
  output_a_target: float = 0.

  def __init__(self):
    self.params = Params()
    self.frame = -1
    self.long_enabled = False
    self.long_override = False
    self.is_enabled = False
    self.is_active = False
    self.enabled = self.params.get_bool("SmartCruiseControlVision")
    self.v_cruise_setpoint = 0.

    self.state = VisionState.disabled
    self.current_lat_acc = 0.
    self.max_pred_lat_acc = 0.

    # CP 移植：功能菜单可调项（默认值 = 上游行为）
    self._a_lat_reg_max = _A_LAT_REG_MAX_DEFAULT
    self._curve_floor = 0.0           # 弯道最低速度下限（m/s），0 = 不限
    self._max_curve = 1e-6            # 本帧预测最大曲率，供保底速度做安全约束
    self._own_vision_enabled = False  # 「视觉弯道限速」开关

  def get_a_target_from_control(self) -> float:
    return self.a_target

  def get_v_target_from_control(self) -> float:
    if self.is_active:
      v = max(self.v_target, MIN_V)

      # CP 移植：弯道最低速度保底 —— 防止过弯被压得过慢、或在大路口停在半路。
      # 保底值同时受横向加速度安全上限约束：弯道太急时 safe_v 低于保底值，保底自动
      # 失效，仍由物理决定。返回值只参与 planner 的 min() 竞争，比别人大就等于弃权，
      # 所以它不可能让车辆跑过巡航设定速度，也不可能压过任何更保守的候选。
      if self._curve_floor > 0.0:
        safe_v = (_CURVE_FLOOR_MAX_LAT_ACC / max(self._max_curve, 1e-6)) ** 0.5
        v = min(max(v, self._curve_floor), max(safe_v, MIN_V))

      return v + self.a_target * _NO_OVERSHOOT_TIME_HORIZON

    return V_CRUISE_UNSET

  def _update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      # CP 移植：功能菜单的「视觉弯道限速」与 SP 原生开关，任一开启即生效。
      self._own_vision_enabled = self.params.get_bool("VisionTurnSpeedEnabled")
      self.enabled = self.params.get_bool("SmartCruiseControlVision") or self._own_vision_enabled

      # CP 移植：弯道激进程度（100 = 上游默认 2.0 m/s²）
      aggr = _read_int_param(self.params, "TurnSpeedAggressiveness", 100,
                             _AGGRESSIVENESS_MIN, _AGGRESSIVENESS_MAX)
      self._a_lat_reg_max = _A_LAT_REG_MAX_DEFAULT * (aggr / 100.0)

      # CP 移植：弯道最低速度下限（km/h，0 = 不限）
      floor_kph = _read_int_param(self.params, "AutoCurveSpeedLowerLimit", 0, 0, 120)
      self._curve_floor = floor_kph * CV.KPH_TO_MS

  def _update_calculations(self, sm: messaging.SubMaster) -> None:
    if not self.long_enabled:
      return
    else:
      rate_plan = np.array(np.abs(sm['modelV2'].orientationRate.z))
      vel_plan = np.array(sm['modelV2'].velocity.x)

      self.current_lat_acc = self.v_ego ** 2 * abs(sm['controlsState'].curvature)

      # get the maximum lat accel from the model
      predicted_lat_accels = rate_plan * vel_plan
      self.max_pred_lat_acc = np.percentile(predicted_lat_accels, 97)

      # get the maximum curve based on the current velocity
      v_ego = max(self.v_ego, 0.1)  # ensure a value greater than 0 for calculations
      max_curve = self.max_pred_lat_acc / (v_ego**2)
      self._max_curve = max_curve  # CP 移植：留给保底速度做安全约束

      # Get the target velocity for the maximum curve
      self.v_target = (self._a_lat_reg_max / max_curve) ** 0.5

  def _update_state_machine(self) -> tuple[bool, bool]:
    # ENABLED, ENTERING, TURNING, LEAVING, OVERRIDING
    if self.state != VisionState.disabled:
      # longitudinal and feature disable always have priority in a non-disabled state
      if not self.long_enabled or not self.enabled:
        self.state = VisionState.disabled
      elif self.long_override:
        self.state = VisionState.overriding

      else:
        # ENABLED
        if self.state == VisionState.enabled:
          # Do not enter a turn control cycle if the speed is low.
          if self.v_ego <= MIN_V:
            pass
          # If significant lateral acceleration is predicted ahead, then move to Entering turn state.
          elif self.max_pred_lat_acc >= _ENTERING_PRED_LAT_ACC_TH:
            self.state = VisionState.entering

        # OVERRIDING
        elif self.state == VisionState.overriding:
          if not self.long_override:
            self.state = VisionState.enabled

        # ENTERING
        elif self.state == VisionState.entering:
          # Transition to Turning if current lateral acceleration is over the threshold.
          if self.current_lat_acc >= _TURNING_LAT_ACC_TH:
            self.state = VisionState.turning
          # Abort if the predicted lateral acceleration drops
          elif self.max_pred_lat_acc < _ABORT_ENTERING_PRED_LAT_ACC_TH:
            self.state = VisionState.enabled

        # TURNING
        elif self.state == VisionState.turning:
          # Transition to Leaving if current lateral acceleration drops below a threshold.
          if self.current_lat_acc <= _LEAVING_LAT_ACC_TH:
            self.state = VisionState.leaving

        # LEAVING
        elif self.state == VisionState.leaving:
          # Transition back to Turning if current lateral acceleration goes back over the threshold.
          if self.current_lat_acc >= _TURNING_LAT_ACC_TH:
            self.state = VisionState.turning
          # Finish if current lateral acceleration goes below a threshold.
          elif self.current_lat_acc < _FINISH_LAT_ACC_TH:
            self.state = VisionState.enabled

    # DISABLED
    elif self.state == VisionState.disabled:
      if self.long_enabled and self.enabled:
        if self.long_override:
          self.state = VisionState.overriding
        else:
          self.state = VisionState.enabled

    enabled = self.state in ENABLED_STATES
    active = self.state in ACTIVE_STATES

    return enabled, active

  def _update_solution(self) -> float:
    # DISABLED, ENABLED, OVERRIDING
    if self.state not in ACTIVE_STATES:
      # when not overshooting, calculate v_turn as the speed at the prediction horizon when following
      # the smooth deceleration.
      a_target = self.a_ego
    # ENTERING
    elif self.state == VisionState.entering:
      # when not overshooting, target a smooth deceleration in preparation for a sharp turn to come.
      a_target = np.interp(self.max_pred_lat_acc, _ENTERING_SMOOTH_DECEL_BP, _ENTERING_SMOOTH_DECEL_V)
    # TURNING
    elif self.state == VisionState.turning:
      # When turning, we provide a target acceleration that is comfortable for the lateral acceleration felt.
      a_target = np.interp(self.current_lat_acc, _TURNING_ACC_BP, _TURNING_ACC_V)
    # LEAVING
    elif self.state == VisionState.leaving:
      # When leaving, we provide a comfortable acceleration to regain speed.
      a_target = _LEAVING_ACC
    else:
      raise NotImplementedError(f"SCC-V state not supported: {self.state}")

    return a_target

  def update(self, sm: messaging.SubMaster, long_enabled: bool, long_override: bool, v_ego: float, a_ego: float,
             v_cruise_setpoint: float) -> None:
    self.long_enabled = long_enabled
    self.long_override = long_override
    self.v_ego = v_ego
    self.a_ego = a_ego
    self.v_cruise_setpoint = v_cruise_setpoint

    self._update_params()
    self._update_calculations(sm)

    self.is_enabled, self.is_active = self._update_state_machine()
    self.a_target = self._update_solution()

    self.output_v_target = self.get_v_target_from_control()
    self.output_a_target = self.get_a_target_from_control()

    self.frame += 1
