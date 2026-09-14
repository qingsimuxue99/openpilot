"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

"""
CP(carrot fork)移植:红绿灯 / 停止标志「虚拟停止线障碍物」。

功能一句话:用驾驶模型自己预测的行驶轨迹(不需要额外的红绿灯辨识模型、不需要地图、
不需要导航)侦测「车辆正在自然减速趋向停止」这个信号,在轨迹终点放一个虚拟的静止
障碍物喂给纵向 MPC,让车辆像跟一台停在那里的车一样自然煞停。

规格来源:traffic_stop_complete_guide_v2.md
  第 3 节 纯函数层(数值与 CP 官方测试对齐)
  第 4 节 常数表
  第 5 节 状态机 + 红绿灯判定(check_model_stopping)
  第 6 节 距离计算流程(median -> moving-average -> rate limit -> 累加器 -> 两层修正)
  第 7 节 架构 B(MPC 注入虚拟障碍物 + candidate min() 第二层软限速)
  第 8.13 节 模式切换时候选来源交接的 taper 保护
"""

import numpy as np
from collections import deque
from enum import IntEnum

from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.common.swaglog import cloudlog

# ---------------------------------------------------------------------------
# 第 4 节:完整常数表
# ---------------------------------------------------------------------------
TRAFFIC_STOP_ENTRY_STEERING_LIMIT_DEG = 50.0     # 大角度转向时视为「在转弯,不是要停等」,只挡新的进入

TRAFFIC_STOP_DISTANCE_RATIO_SPEED_BP_KPH = (0.0, 100.0)
TRAFFIC_STOP_DISTANCE_RATIO = (1.0, 0.7)         # 车速越快,煞车起点比例越往内拉
TRAFFIC_STOP_DISTANCE_FADE_BP_M = (0.0, 50.0)    # 停止线 50m 以内比例渐变回 100%

COMFORT_BRAKE_BASE = 2.4                         # 注意:是 2.4,不是 2.5
COMFORT_BRAKE_DISCOUNT = 0.9                     # 只在 STOPPING 阶段打 9 折,STOPPED 不套用
STOPPED_V_EGO_THRESHOLD = 0.3                    # 已停止判定(m/s),单帧触发
STOPPED_GRACE_SECONDS = 0.5                      # 完全停止后的绿灯冷却期
GREEN_DEBOUNCE_SECONDS = 0.2                     # 绿灯确认防抖(红灯没有防抖)
GAS_SUPPRESS_SECONDS = 10.0                      # 油门覆写后的重新侦测抑制
ACCUMULATOR_UPDATE_MIN_M = 10.0                  # 累加器只在候选距离 > 10m 时才重新校正
LEAD_CANCEL_MARGIN_M = 2.0                       # 前车缩短到停止线 2m 内才取消已进行的停等
MEDIAN_WINDOW = 3
AVERAGE_WINDOW = 15
SPEED_AVERAGE_WINDOW = 10
RATE_LIMIT_EXTRA_M = 0.5                         # rate limiter 靠近速度上限 = v_ego*dt + 0.5
STOP_SIGN_MAX_SPEED_KPH = 82.0                   # 停止标志侦测上限车速
STOP_SIGN_DETECT_DIST_BP_KPH = (60.0, 80.0)
STOP_SIGN_DETECT_DIST_M = (120.0, 150.0)         # 60kph->120m, 80kph->150m 线性内插
STOP_SIGN_LATERAL_TOLERANCE_M = 5.0              # 停止标志侧向误差容许
MAX_OBSTACLE_DISTANCE_M = 300.0                  # 超过此距离不产生软限速

# 第 8.12 节:距离修正拆成两层
#   1) 固定物理修正(相机/装置安装位置到车头保桿),写死在程式码里,无条件套用
#   2) 使用者微调(UI 参数),预设 0,叠加在上面
# 注:-1.5m 是 CP 针对其硬体/安装位置调校出来的值,不是普适物理常数;
#     若你的车实际停得太远/太近,用 UI 微调补偿即可,不要改这里。
CAMERA_TO_FRONT_M = -1.5
DISTANCE_ADJUST_LIMIT_M = 5.0

PARAM_REFRESH_FRAMES = 20                        # 每秒(20Hz)轮询一次 Params

# 帧数换算(用 round 避免 0.2/0.05 -> 3.9999 的浮点陷阱)
STOPPED_GRACE_FRAMES = int(round(STOPPED_GRACE_SECONDS / DT_MDL))
GREEN_DEBOUNCE_FRAMES = int(round(GREEN_DEBOUNCE_SECONDS / DT_MDL))
GAS_SUPPRESS_FRAMES = int(round(GAS_SUPPRESS_SECONDS / DT_MDL))


class TrafficState(IntEnum):
  """红绿灯侦测结果。"""
  off = 0
  red = 1
  green = 2


class TrafficStopState(IntEnum):
  """第 5.1 节三态定义。"""
  CRUISE = 0     # 正常巡航,没有虚拟障碍物
  STOPPING = 1   # 正在煞车接近停止线
  STOPPED = 2    # 已完全停止


# ---------------------------------------------------------------------------
# 第 3 节:纯函数层(可直接单元测试,数值已对齐 CP 官方测试)
# ---------------------------------------------------------------------------
def is_traffic_stop_entry_allowed(steering_angle_deg: float) -> bool:
  """大角度转向时视为「这是在转弯,不是要停等」,只用來挡新的进入,不影响已在停等中的状态。"""
  return abs(steering_angle_deg) < TRAFFIC_STOP_ENTRY_STEERING_LIMIT_DEG


def get_traffic_stop_reference_speed(v_ego_kph: float, previous_reference_kph: float | None) -> float:
  """锁存本次停车过程中出现过的最高车速(只会越锁越高,不会下降)。"""
  return max(0.0, v_ego_kph, previous_reference_kph or 0.0)


def get_virtual_traffic_stop_distance(model_distance: float, v_ego_kph: float) -> float:
  """车速越快,煞车起点比例越往内拉(0kph=100%, 100kph=70%),
  但在停止线本身 50m 以内,比例会渐变回 100%,确保最终真的停在正确位置。"""
  distance_ratio = np.interp(v_ego_kph, TRAFFIC_STOP_DISTANCE_RATIO_SPEED_BP_KPH, TRAFFIC_STOP_DISTANCE_RATIO)
  applied_ratio = np.interp(model_distance, TRAFFIC_STOP_DISTANCE_FADE_BP_M, [1.0, distance_ratio])
  return float(max(0.0, model_distance * applied_ratio))


def get_traffic_stop_obstacle_distance(stop_distance: float, distance_adjust: float) -> float:
  """套用距离修正(固定物理修正 + 使用者微调,单纯相加 + 下限 0)。
  注意:这个函数在整条资料流里只能被呼叫一次(第 8.3 项)。"""
  return float(max(0.0, stop_distance + distance_adjust))


def taper_toward_less_conservative_output(candidate_min: float, prev_output: float,
                                          j_taper: float, dt: float) -> float:
  """第 8.13 项:只限制「输出变得比上一帧更不保守(更想加速)」的方向。
  任何比 prev_output 更保守的候选值,min() 本身就会立刻选中它,不受这个函数限制。
  因此这个修正绝对不可能延迟任何真正需要的紧急煞车。"""
  return float(min(candidate_min, prev_output + j_taper * dt))


class _MovingWindow:
  """定长滑动窗口。median=True 取中位数,否则取平均值。

  第 8.9 项:这三个滤波器跨停等事件持续运作,**永远不清空**。
  """

  def __init__(self, size: int, median: bool = False):
    self._buf: deque[float] = deque(maxlen=size)
    self._median = median

  def process(self, value: float) -> float:
    self._buf.append(float(value))
    if self._median:
      return float(np.median(self._buf))
    return float(np.mean(self._buf))

  def clear(self) -> None:
    self._buf.clear()


# ---------------------------------------------------------------------------
# 第 5 节:状态机
# ---------------------------------------------------------------------------
class TrafficStopController:
  """红绿灯/停止标志虚拟停止线控制器。

  每帧被纵向规划器呼叫一次,回传 (stop_dist_m, v_cruise_limited):
    - stop_dist_m:要喂给纵向 MPC 的虚拟静止障碍物距离(米),None = 不注入
    - v_cruise_limited:软限速(米/秒),None = 不限制
  """

  def __init__(self, CP, dt: float = DT_MDL):
    self.CP = CP
    self.dt = dt
    self.params = Params()
    self._frame = -1

    # 参数状态
    self.enabled = False
    self.distance_adjust_m = 0.0
    self._param_read_failed = False

    # 状态机(第 5.1 节)
    self.state = TrafficStopState.CRUISE
    self.reference_speed_kph = 0.0
    self.actual_stop_distance = 0.0
    self.stopped_grace_frames = 0
    self.gas_suppress_frames = 0

    # 红绿灯判定
    self.traffic_state = TrafficState.off
    self.stop_sign_count = 0
    self.start_sign_count = 0

    # 距离链(第 6 节)
    self._stop_x_rl: float | None = None
    self._x_median = _MovingWindow(MEDIAN_WINDOW, median=True)
    self._x_average = _MovingWindow(AVERAGE_WINDOW)
    self._v_average = _MovingWindow(SPEED_AVERAGE_WINDOW)

    self._refresh_params()

  # -- 参数 --------------------------------------------------------------
  def _refresh_params(self) -> None:
    try:
      self.enabled = bool(self.params.get_bool("TrafficStopEnabled"))
      raw_adjust = float(self.params.get("TrafficStopDistanceAdjust", return_default=True))
      self.distance_adjust_m = float(np.clip(raw_adjust, -DISTANCE_ADJUST_LIMIT_M, DISTANCE_ADJUST_LIMIT_M))
      self._param_read_failed = False
    except Exception as e:
      self.enabled = False
      self.distance_adjust_m = 0.0
      if not self._param_read_failed:
        cloudlog.warning(f"[TrafficStop] 读取参数失败,功能已停用: {type(e).__name__}: {e}")
      self._param_read_failed = True

  # -- 状态 --------------------------------------------------------------
  @property
  def active(self) -> bool:
    """是否正在主动停等(虚拟障碍物存在)。"""
    return self.state != TrafficStopState.CRUISE

  def reset(self) -> None:
    """重置状态机。注意:**不清空滤波器**(第 8.9 项)。"""
    self.state = TrafficStopState.CRUISE
    self.reference_speed_kph = 0.0
    self.actual_stop_distance = 0.0
    self.stopped_grace_frames = 0
    self.gas_suppress_frames = 0
    self.traffic_state = TrafficState.off
    self.stop_sign_count = 0
    self.start_sign_count = 0
    self._stop_x_rl = None

  # -- 第 6 节:距离链 ----------------------------------------------------
  def _update_stop_model_x(self, raw_x: float, v_ego: float) -> tuple[float, float]:
    """median(3) -> moving-average(15) = stop_model_x_raw
       -> rate limiter(只限制「变近」的速度,「变远」立刻放行) = stop_model_x_rl"""
    stop_x = self._x_median.process(raw_x)
    stop_x = self._x_average.process(stop_x)
    stop_model_x_raw = stop_x

    if self._stop_x_rl is None:
      self._stop_x_rl = stop_model_x_raw
    else:
      max_close = v_ego * self.dt + RATE_LIMIT_EXTRA_M
      if stop_model_x_raw > self._stop_x_rl:
        self._stop_x_rl = stop_model_x_raw
      else:
        self._stop_x_rl = max(self._stop_x_rl - max_close, stop_model_x_raw)

    return stop_model_x_raw, self._stop_x_rl

  # -- 第 5.3 节:红绿灯侦测 ----------------------------------------------
  def _check_model_stopping(self, v_cruise: float, model_v_traj, v_ego: float, a_ego: float,
                            model_x_end: float, model_y_traj, d_rel: float) -> TrafficState:
    v_ego_kph = v_ego * CV.MS_TO_KPH
    model_v = self._v_average.process(float(model_v_traj[-1]))
    model_v_start = float(model_v_traj[0])

    start_sign = model_v > 5.0 or model_v > (model_v_start + 2.0)

    if v_ego_kph < 1.0:
      stop_sign = model_x_end < 20.0 and model_v < 10.0
    elif v_ego_kph < STOP_SIGN_MAX_SPEED_KPH:
      max_detect_dist = float(np.interp(model_v_start * CV.MS_TO_KPH,
                                        STOP_SIGN_DETECT_DIST_BP_KPH, STOP_SIGN_DETECT_DIST_M))
      stop_sign = (model_x_end < d_rel - 3.0 and
                   model_x_end < max_detect_dist and
                   (model_v < 3.0 or model_v < model_v_start * 0.7) and
                   abs(float(model_y_traj[-1])) < STOP_SIGN_LATERAL_TOLERANCE_M)
      # 正常行驶中的减速(例如前方弯道、坡道)误判很多;
      # 但 v_cruise == 0(车辆自己决定要停)时仍要侦测红绿灯。
      if v_cruise != 0 and self.state == TrafficStopState.CRUISE and a_ego < -1.0:
        stop_sign = False
    else:
      stop_sign = False

    self.stop_sign_count = self.stop_sign_count + 1 if stop_sign else 0
    self.start_sign_count = self.start_sign_count + 1 if (start_sign and not stop_sign) else 0

    if self.stop_sign_count * self.dt > 0.0:
      return TrafficState.red
    if self.start_sign_count * self.dt > GREEN_DEBOUNCE_SECONDS:
      return TrafficState.green
    return TrafficState.off

  # -- 主入口 ------------------------------------------------------------
  def update(self, sm, v_ego: float, a_ego: float, v_cruise: float, engaged: bool):
    """每个 planner 控制周期呼叫一次。回传 (stop_dist_m, v_cruise_limited)。"""
    self._frame += 1
    if self._frame % PARAM_REFRESH_FRAMES == 0:
      self._refresh_params()

    if not self.enabled:
      self.reset()
      return None, None

    model_v2 = sm['modelV2']
    model_x_traj = model_v2.position.x
    model_y_traj = model_v2.position.y
    model_v_traj = model_v2.velocity.x
    if len(model_x_traj) < 2 or len(model_v_traj) == 0:
      return None, None

    car_state = sm['carState']
    lead = sm['radarState'].leadOne

    lead_present = bool(lead.present)
    d_rel = float(lead.dRel) if lead_present else 1000.0
    steering_angle_deg = float(car_state.steeringAngleDeg)
    gas_pressed = bool(car_state.gasPressed)
    left_blinker = bool(car_state.leftBlinker)

    stop_model_x_raw, stop_model_x_rl = self._update_stop_model_x(float(model_x_traj[-2]), v_ego)

    self.traffic_state = self._check_model_stopping(v_cruise, model_v_traj, v_ego, a_ego,
                                                    float(model_x_traj[-1]), model_y_traj, d_rel)

    # 第 8.11 项:油门抑制计时器只在「已在停等中踩油门覆写」才武装
    if gas_pressed and self.state == TrafficStopState.STOPPING:
      self.gas_suppress_frames = GAS_SUPPRESS_FRAMES
    elif self.gas_suppress_frames > 0:
      self.gas_suppress_frames -= 1

    # 第 8.4 项:取消已进行的停等用 2m margin;进入新停等则是「只要有前车就挡」
    lead_closer_than_stop = lead_present and (d_rel - stop_model_x_raw) < LEAD_CANCEL_MARGIN_M

    if self.state == TrafficStopState.CRUISE:
      entry_allowed = is_traffic_stop_entry_allowed(steering_angle_deg)
      if (not lead_present and self.traffic_state == TrafficState.red and
              entry_allowed and self.gas_suppress_frames == 0):
        self.state = TrafficStopState.STOPPING
        self.reference_speed_kph = get_traffic_stop_reference_speed(v_ego * CV.MS_TO_KPH, None)
        self.actual_stop_distance = get_virtual_traffic_stop_distance(stop_model_x_rl, self.reference_speed_kph)

    elif self.state == TrafficStopState.STOPPING:
      if gas_pressed:
        self.state = TrafficStopState.CRUISE
      elif lead_closer_than_stop:
        self.state = TrafficStopState.CRUISE
      elif self.traffic_state == TrafficState.green:
        self.state = TrafficStopState.CRUISE
      else:
        self.reference_speed_kph = get_traffic_stop_reference_speed(v_ego * CV.MS_TO_KPH, self.reference_speed_kph)
        candidate = get_virtual_traffic_stop_distance(stop_model_x_rl, self.reference_speed_kph)
        if candidate > ACCUMULATOR_UPDATE_MIN_M:
          self.actual_stop_distance = candidate
        if v_ego < STOPPED_V_EGO_THRESHOLD:      # 第 8.6 项:单帧触发
          self.state = TrafficStopState.STOPPED
          self.stopped_grace_frames = STOPPED_GRACE_FRAMES

    elif self.state == TrafficStopState.STOPPED:
      if gas_pressed:
        self.state = TrafficStopState.CRUISE
      elif lead_closer_than_stop:
        self.state = TrafficStopState.CRUISE
      else:
        # 第 8.7 项:绿灯冷却期是独立机制,跟「障碍物瞬间释放」分开
        if self.stopped_grace_frames == 0:
          if self.traffic_state == TrafficState.green and not left_blinker:
            self.state = TrafficStopState.CRUISE
        self.stopped_grace_frames = max(0, self.stopped_grace_frames - 1)

    # 障碍物瞬间释放:独立于状态机,不能共用同一段 reset
    if self.state == TrafficStopState.CRUISE:
      self.actual_stop_distance = 0.0
      self.reference_speed_kph = 0.0
      self.stopped_grace_frames = 0
      return None, None

    # dead-reckoning 累加器倒数(第 8.10 项)
    self.actual_stop_distance = max(0.0, self.actual_stop_distance - v_ego * self.dt)

    if self.traffic_state in (TrafficState.off, TrafficState.green):
      self.actual_stop_distance = 0.0
      return None, None                        # state 维持原样不变

    # 主动停等中:每帧强制把 rate limiter 状态同步为 raw
    self._stop_x_rl = stop_model_x_raw

    contribution = 0.0 if self.actual_stop_distance > 0.0 else stop_model_x_rl
    stop_dist = max(0.0, contribution + self.actual_stop_distance)
    stop_dist = get_traffic_stop_obstacle_distance(stop_dist, CAMERA_TO_FRONT_M + self.distance_adjust_m)

    # 第 8.5 项:STOPPED 状态下 v_cruise 硬性归零
    if self.state == TrafficStopState.STOPPED:
      v_cruise_limited = 0.0
    else:
      v_cruise_limited = None
      if stop_dist < MAX_OBSTACLE_DISTANCE_M:
        # 第 8.2 项:只有在 STOPPING(还在煞车中)才打 9 折
        comfort_brake = COMFORT_BRAKE_BASE * COMFORT_BRAKE_DISCOUNT
        v_cruise_limited = (2.0 * comfort_brake * max(stop_dist - 1.0, 0.0)) ** 0.5
        v_cruise_limited = min(v_cruise_limited, v_ego)   # 第 10 节第 12 项:不超过目前车速

    # 未接管(未进入纵向控制)时不注入障碍物、不限制 v_cruise;
    # 但状态机与滤波器照常运作,这样一接管就能立刻生效。
    if not engaged:
      return None, None

    return float(stop_dist), float(v_cruise_limited) if v_cruise_limited is not None else None
