import json
import math
import os
import numpy as np
from cereal import log
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
# from openpilot.common.swaglog import cloudlog
# from openpilot.common.logger import sLogger
from openpilot.common.params import Params

TRAJECTORY_SIZE = 33
# positive numbers go right
CAMERA_OFFSET = 0 #0.08
MIN_LANE_DISTANCE = 2.6
MAX_LANE_DISTANCE = 3.7
MAX_LANE_CENTERING_AWAY = 1.85
KEEP_MIN_DISTANCE_FROM_LANE = 1.35
KEEP_MIN_DISTANCE_FROM_EDGELANE = 1.15

def clamp(num, min_value, max_value):
  # weird broken case, do something reasonable
  if min_value > num > max_value:
    return (min_value + max_value) * 0.5
  # ok, basic min/max below
  if num < min_value:
    return min_value
  if num > max_value:
    return max_value
  return num

def sigmoid(x, scale=1, offset=0):
  return (1 / (1 + math.exp(x*scale))) + offset

def lerp(start, end, t):
  t = clamp(t, 0.0, 1.0)
  return (start * (1.0 - t)) + (end * t)

def max_abs(a, b):
  return a if abs(a) > abs(b) else b

class LanePlanner:
  def __init__(self):
    self.ll_t = np.zeros((TRAJECTORY_SIZE,))
    self.ll_x = np.zeros((TRAJECTORY_SIZE,))
    self.lll_y = np.zeros((TRAJECTORY_SIZE,))
    self.rll_y = np.zeros((TRAJECTORY_SIZE,))
    self.le_y = np.zeros((TRAJECTORY_SIZE,))
    self.re_y = np.zeros((TRAJECTORY_SIZE,))
    #self.lane_width_estimate = FirstOrderFilter(3.2, 9.95, DT_MDL)
    self.lane_width_estimate = FirstOrderFilter(3.2, 3.0, DT_MDL)
    self.lane_width = 3.2
    self.lane_width_last = self.lane_width
    self.lane_change_multiplier = 1
    #self.lane_width_updated_count = 0

    self.lll_prob = 0.
    self.rll_prob = 0.
    self.d_prob = 0.

    self.lll_std = 0.
    self.rll_std = 0.

    self.l_lane_change_prob = 0.
    self.r_lane_change_prob = 0.

    self.debugText = ""
    self.lane_width_left = 0.0
    self.lane_width_right = 0.0
    self.lane_width_left_filtered = FirstOrderFilter(1.0, 1.0, DT_MDL)
    self.lane_width_right_filtered = FirstOrderFilter(1.0, 1.0, DT_MDL)
    self.lane_offset_filtered = FirstOrderFilter(0.0, 2.0, DT_MDL)

    self.lanefull_mode = False
    self.d_prob_count = 0

    self.params = Params()

    # === 大路口动态偏移限制 (硬编码参数) ===
    self.curveOffsetLimit = 0.15       # 最大偏移限制(m), 0=关闭
    self.curveSpeedThreshold = 70      # curve_speed阈值, 超过此值视为大路口

    # === AutoCenter: 自动居中纠偏 (auto lane centering correction) ===
    # 0: off, 1: realtime correction only, 2: realtime + long-term learning (persisted)
    self.ac_enabled = 2
    self.ac_gain = 0.4                                        # P gain for realtime correction
    self.ac_error_filtered = FirstOrderFilter(0.0, 1.0, DT_MDL)  # 1s low-pass on measured error
    self.ac_learned = 0.0                                     # slow-learned static offset (m)
    self.ac_learned_saved = 0.0
    self.ac_applied = 0.0                                     # rate-limited output (m)
    self.ac_param_ok = False                                  # params registered? else json file fallback
    self.ac_read_counter = 0
    self.ac_save_counter = 0
    self._ac_load()

  AC_FILE = "/data/carrot_auto_center.json"
  AC_LEARN_LIMIT = 0.15      # max learned static offset (m)
  AC_P_LIMIT = 0.20          # max realtime correction (m)
  AC_TOTAL_LIMIT = 0.30      # max total correction (m)
  AC_LEARN_TAU = 45.0        # learning time constant (s)
  AC_RATE_LIMIT = 0.06       # max output slew rate (m/s)

  def _ac_read_config(self):
    # prefer Params (needs key registration + rebuild); fallback to json file (no rebuild needed)
    try:
      self.ac_enabled = self.params.get_int("AutoLaneCorrection")
      gain = self.params.get_int("AutoLaneCorrectionGain")
      self.ac_gain = clamp(gain, 10, 100) * 0.01 if gain > 0 else 0.4
      self.ac_param_ok = True
    except Exception:
      self.ac_param_ok = False
      try:
        if os.path.exists(self.AC_FILE):
          with open(self.AC_FILE) as f:
            d = json.load(f)
          self.ac_enabled = int(d.get("enabled", 2))
          self.ac_gain = clamp(float(d.get("gain", 40)), 10, 100) * 0.01
      except Exception:
        pass

  def _ac_load(self):
    self._ac_read_config()
    try:
      if self.ac_param_ok:
        self.ac_learned = clamp(self.params.get_float("AutoLaneCorrectionLearned"), -self.AC_LEARN_LIMIT, self.AC_LEARN_LIMIT)
      elif os.path.exists(self.AC_FILE):
        with open(self.AC_FILE) as f:
          d = json.load(f)
        self.ac_learned = clamp(float(d.get("learned", 0.0)), -self.AC_LEARN_LIMIT, self.AC_LEARN_LIMIT)
    except Exception:
      self.ac_learned = 0.0
    self.ac_learned_saved = self.ac_learned

  def _ac_save(self):
    try:
      if self.ac_param_ok:
        self.params.put_float_nonblocking("AutoLaneCorrectionLearned", float(self.ac_learned))
      else:
        with open(self.AC_FILE, "w") as f:
          json.dump({"enabled": self.ac_enabled, "gain": round(self.ac_gain * 100), "learned": round(self.ac_learned, 4)}, f)
      self.ac_learned_saved = self.ac_learned
    except Exception:
      pass

  def update_auto_center(self, CS, v_ego):
    # re-read config every ~5s so panel/file changes apply on the fly
    self.ac_read_counter += 1
    if self.ac_read_counter >= 100:
      self.ac_read_counter = 0
      self._ac_read_config()

    if self.ac_enabled <= 0:
      self.ac_error_filtered.x = 0.0
      self.ac_applied = 0.0
      return 0.0

    # measurement gating: only trust confident, sane lane lines while driving straight-ish
    lane_width_now = self.rll_y[0] - self.lll_y[0]
    meas_ok = (
      self.lll_prob > 0.5 and self.rll_prob > 0.5 and
      self.lll_std < 0.3 and self.rll_std < 0.3 and
      2.5 < lane_width_now < 4.6 and
      self.lane_change_multiplier > 0.5 and
      not CS.steeringPressed and
      v_ego * 3.6 > 15.0
    )

    if meas_ok:
      # e > 0: lane center is to the right of car -> shift path right (y positive = right in this fork)
      error = clamp((self.lll_y[0] + self.rll_y[0]) * 0.5, -0.6, 0.6)
      self.ac_error_filtered.update(error)
      if self.ac_enabled >= 2:
        # slow integrator removes steady-state bias (camera mount / steering bias)
        self.ac_learned = clamp(self.ac_learned + self.ac_error_filtered.x * (DT_MDL / self.AC_LEARN_TAU),
                                -self.AC_LEARN_LIMIT, self.AC_LEARN_LIMIT)
    else:
      self.ac_error_filtered.update(0.0)

    # persist learned offset every ~30s when changed > 5mm
    self.ac_save_counter += 1
    if self.ac_save_counter >= 600:
      self.ac_save_counter = 0
      if abs(self.ac_learned - self.ac_learned_saved) > 0.005:
        self._ac_save()

    p_term = clamp(self.ac_error_filtered.x * self.ac_gain, -self.AC_P_LIMIT, self.AC_P_LIMIT)
    target = clamp(p_term + self.ac_learned, -self.AC_TOTAL_LIMIT, self.AC_TOTAL_LIMIT)
    max_step = self.AC_RATE_LIMIT * DT_MDL
    self.ac_applied = clamp(target, self.ac_applied - max_step, self.ac_applied + max_step)
    return self.ac_applied

  def parse_model(self, md):

    lane_lines = md.laneLines
    edges = md.roadEdges

    if len(lane_lines) >= 4 and len(lane_lines[0].t) == TRAJECTORY_SIZE:
      self.ll_t = (np.array(lane_lines[1].t) + np.array(lane_lines[2].t))/2
      # left and right ll x is the same
      self.ll_x = lane_lines[1].x
      self.lll_y = np.array(lane_lines[1].y)
      self.rll_y = np.array(lane_lines[2].y)
      self.lll_prob = md.laneLineProbs[1]
      self.rll_prob = md.laneLineProbs[2]
      self.lll_std = md.laneLineStds[1]
      self.rll_std = md.laneLineStds[2]

    if len(edges[0].t) == TRAJECTORY_SIZE:
      self.le_y = np.array(edges[0].y) + md.roadEdgeStds[0] * 0.4
      self.re_y = np.array(edges[1].y) - md.roadEdgeStds[1] * 0.4
    else:
      self.le_y = self.lll_y
      self.re_y = self.rll_y

    desire_state = md.meta.desireState
    if len(desire_state):
      self.l_lane_change_prob = desire_state[log.Desire.laneChangeLeft]
      self.r_lane_change_prob = desire_state[log.Desire.laneChangeRight]

  def get_d_path(self, CS, v_ego, path_t, path_xyz, curve_speed):
    #if v_ego > 0.1:
    #  self.lane_width_updated_count = max(0, self.lane_width_updated_count - 1)
    # Reduce reliance on lanelines that are too far apart or
    # will be in a few seconds
    l_prob, r_prob = self.lll_prob, self.rll_prob
    width_pts = self.rll_y - self.lll_y
    prob_mods = []
    for t_check in (0.0, 1.5, 3.0):
      width_at_t = np.interp(t_check * (v_ego + 7), self.ll_x, width_pts)
      #prob_mods.append(np.interp(width_at_t, [4.0, 5.0], [1.0, 0.0]))
      prob_mods.append(np.interp(width_at_t, [4.5, 6.0], [1.0, 0.0]))
    mod = min(prob_mods)
    l_prob *= mod
    r_prob *= mod

    # Reduce reliance on uncertain lanelines
    l_std_mod = np.interp(self.lll_std, [.15, .3], [1.0, 0.0])
    r_std_mod = np.interp(self.rll_std, [.15, .3], [1.0, 0.0])
    l_prob *= l_std_mod
    r_prob *= r_std_mod

    self.l_prob, self.r_prob = l_prob, r_prob

    # Find current lanewidth
    current_lane_width = abs(self.rll_y[0] - self.lll_y[0])

    max_updated_count = 10.0 * DT_MDL
    both_lane_available = False
    #speed_lane_width = np.interp(v_ego*3.6, [0., 60.], [2.8, 3.5])
    if l_prob > 0.5 and r_prob > 0.5 and self.lane_change_multiplier > 0.5:
      both_lane_available = True
      #self.lane_width_updated_count = max_updated_count
      self.lane_width_estimate.update(current_lane_width)
      self.lane_width_last = self.lane_width_estimate.x
    #elif self.lane_width_updated_count <= 0 and v_ego > 0.1:   # 양쪽차선이 없을때.... 일정시간후(10초)부터 speed차선폭 적용함.
    #  self.lane_width_estimate.update(speed_lane_width)
    else:
      self.lane_width_estimate.update(self.lane_width_last)

    self.lane_width =  self.lane_width_estimate.x

    clipped_lane_width = min(4.0, self.lane_width)
    path_from_left_lane = self.lll_y + clipped_lane_width / 2.0
    path_from_right_lane = self.rll_y - clipped_lane_width / 2.0

    # 가장 차선이 진한쪽으로 골라서..
    self.d_prob = max(l_prob, r_prob) if not both_lane_available else 1.0

    # 좌/우의 차선폭을 필터링.
    if self.lane_width_left > 0:
      self.lane_width_left_filtered.update(self.lane_width_left)
      #self.lane_width_left_filtered.x = self.lane_width_left #바로적용
    if self.lane_width_right > 0:
      self.lane_width_right_filtered.update(self.lane_width_right)
      #self.lane_width_right_filtered.x = self.lane_width_right #바로적용

    self.adjustLaneOffset = float(self.params.get_int("AdjustLaneOffset")) * 0.01
    self.adjustCurveOffset = float(self.params.get_int("AdjustCurveOffset")) * 0.01
    #self.adjustCurveOffset = self.adjustLaneOffset #float(self.params.get_int("AdjustCurveOffset")) * 0.01

    # === 大路口动态偏移限制 (硬编码参数) ===
    # self.curveOffsetLimit 和 self.curveSpeedThreshold 已在 __init__ 中初始化

    ADJUST_OFFSET_LIMIT = 0.4 #max(self.adjustLaneOffset, self.adjustCurveOffset)
    offset_curve = 0.0
    ## curve offset
    offset_curve = np.interp(abs(curve_speed), [50, 200], [self.adjustCurveOffset, 0.0]) * np.sign(curve_speed)

    offset_lane = 0.0
    if self.lane_width_left_filtered.x > 2.2 and self.lane_width_right_filtered.x > 2.2: #양쪽에 차로가 여유 있는경우
      offset_lane = 0.0
    elif self.lane_width_left_filtered.x < 2.0 and self.lane_width_right_filtered.x < 2.0: #양쪽에 차로가 여유 없는경우
      offset_lane = 0.0
    elif self.lane_width_left_filtered.x > self.lane_width_right_filtered.x:
      offset_lane = np.interp(self.lane_width, [2.5, 2.9], [0.0, self.adjustLaneOffset]) # 차선이 좁으면 안함..
    else:
      offset_lane = np.interp(self.lane_width, [2.5, 2.9], [0.0, -self.adjustLaneOffset]) # 차선이 좁으면 안함..

    #select lane path
    # 차선이 좁아지면, 도로경계쪽에 있는 차선 위주로 따라가도록함.
    if self.lane_width < 2.5:
      if r_prob > 0.5 and self.lane_width_right_filtered.x < self.lane_width_left_filtered.x:
        lane_path_y = path_from_right_lane
      elif l_prob > 0.5 and self.lane_width_left_filtered.x < 2.0:
        lane_path_y = path_from_left_lane
      else:
        lane_path_y = path_from_left_lane if l_prob > 0.5 or l_prob > r_prob else path_from_right_lane
    elif l_prob > 0.7 and r_prob > 0.7:
      lane_path_y = (path_from_left_lane + path_from_right_lane) / 2.
      # lane_width filtering에 의해서, 점점 줄어들때, 중앙선으로 붙어가는 현상이 생김..
      #if self.lane_width > 3.2:
      #  lane_path_y = path_from_right_lane
      #else:
      #  lane_path_y = (path_from_left_lane + path_from_right_lane) / 2.
    # 그외 진한차선을 따라가도록함.
    else:
      lane_path_y = (l_prob * path_from_left_lane + r_prob * path_from_right_lane) / (l_prob + r_prob + 0.0001)

    use_laneless_center_adjust = False
    if use_laneless_center_adjust:
      ## 0.5초 앞의 중심을 보도록함.
      lane_path_y_center = np.interp(0.5, path_t, lane_path_y)
      path_xyz_y_center = np.interp(0.5, path_t, path_xyz[:,1])
      #lane_path_y_center = lane_path_y[0]
      #path_xyz_y_center = path_xyz[:,1][0]
      diff_center = (lane_path_y_center - path_xyz_y_center) if not self.lanefull_mode else 0.0
    else:
      diff_center = 0.0
    #print("center = {:.2f}={:.2f}-{:.2f}, lanefull={}".format(diff_center, lane_path_y_center, path_xyz_y_center, self.lanefull_mode))
    #diff_center = lane_path_y[5] - path_xyz[:,1][5] if not self.lanefull_mode else 0.0
    if offset_curve * offset_lane < 0:
      offset_total = np.clip(offset_curve + offset_lane + diff_center, - ADJUST_OFFSET_LIMIT, ADJUST_OFFSET_LIMIT)
    else:
      offset_total = np.clip(max(offset_curve, offset_lane, key=abs) + diff_center, - ADJUST_OFFSET_LIMIT, ADJUST_OFFSET_LIMIT)

    ## self.d_prob = 0 if lane_changing
    self.d_prob *= self.lane_change_multiplier  ## 차선변경중에는 꺼버림.
    if self.lane_change_multiplier < 0.5:
      #self.lane_offset_filtered.x = 0.0
      pass
    else:
      self.lane_offset_filtered.update(np.interp(self.d_prob, [0, 0.3], [0, offset_total]))

    # === 大路口动态偏移限制 ===
    # curve_speed 大 = 曲率小 = 大路口(弯道平缓)
    # 仅在 curveOffsetLimit > 0(功能开启) 且 curve_speed 超过阈值时生效
    if self.curveOffsetLimit > 0 and abs(curve_speed) > self.curveSpeedThreshold:
      if curve_speed > 0:    # 左转（大路口）：限制向左的最大偏移
        self.lane_offset_filtered.x = min(self.lane_offset_filtered.x, self.curveOffsetLimit)
      elif curve_speed < 0:  # 右转（大路口）：限制向右的最大偏移
        self.lane_offset_filtered.x = max(self.lane_offset_filtered.x, -self.curveOffsetLimit)

    ## laneless at lowspeed
    self.d_prob *= np.interp(v_ego*3.6, [5., 15.], [0.0, 1.0])

    #self.debugText = "OFFSET({:.2f}={:.2f}+{:.2f}+{:.2f}),Vc:{:.2f},dp:{:.1f},lf:{},lrw={:.1f}|{:.1f}|{:.1f}".format(
    #  self.lane_offset_filtered.x,
    #  diff_center, offset_lane, offset_curve,
    #  curve_speed,
    #  self.d_prob, self.lanefull_mode,
    #  self.lane_width_left_filtered.x, self.lane_width, self.lane_width_right_filtered.x)

    adjustLaneTime = self.params.get_float("LatMpcInputOffset") * 0.01 # 0.06
    laneline_active = False
    self.d_prob_count = self.d_prob_count + 1 if self.d_prob > 0.3 else 0
    if self.lanefull_mode and self.d_prob_count > int(1 / DT_MDL):
      laneline_active = True
      use_dist_mode = False  ## 아무리생각해봐도.. 같은 방법인듯...
      if use_dist_mode:
        lane_path_y_interp = np.interp(path_xyz[:,0] + v_ego * adjustLaneTime, self.ll_x, lane_path_y)
        path_xyz[:,1] = self.d_prob * lane_path_y_interp + (1.0 - self.d_prob) * path_xyz[:,1]
      else:
        safe_idxs = np.isfinite(self.ll_t)
        if safe_idxs[0]:
          lane_path_y_interp = np.interp(path_t * (1.0 + adjustLaneTime), self.ll_t[safe_idxs], lane_path_y[safe_idxs])
          path_xyz[:,1] = self.d_prob * lane_path_y_interp + (1.0 - self.d_prob) * path_xyz[:,1]

    # AutoCenter: continuous auto correction towards lane center (works in lane mode AND laneless mode)
    ac_offset = self.update_auto_center(CS, v_ego)

    path_xyz[:, 1] += (CAMERA_OFFSET + self.lane_offset_filtered.x + ac_offset)

    self.offset_total = self.lane_offset_filtered.x + ac_offset

    return path_xyz, laneline_active

  def calculate_plan_yaw_and_yaw_rate(self, path_xyz):
    if path_xyz.shape[0] < 3:
        # 너무 짧으면 직진 가정
        N = path_xyz.shape[0]
        return np.zeros(N), np.zeros(N)

    # x, y 추출
    x = path_xyz[:, 0]
    y = path_xyz[:, 1]

    # 모두 동일한 점인지 확인
    if np.allclose(x, x[0]) and np.allclose(y, y[0]):
        return np.zeros(len(x)), np.zeros(len(x))

    # 안전한 diff 계산
    dx = np.diff(x)
    dy = np.diff(y)
    mask = (dx == 0) & (dy == 0)
    dx[mask] = 1e-4
    dy[mask] = 0.0

    yaw = np.arctan2(dy, dx)
    yaw = np.append(yaw, yaw[-1])  # N-1 → N
    yaw = np.unwrap(yaw)

    dx_full = np.clip(np.diff(x), 1e-4, None)
    yaw_rate = np.diff(yaw) / dx_full
    yaw_rate = np.append(yaw_rate, yaw_rate[-1])
    yaw_rate = np.append(yaw_rate, 0.0)

    # NaN/Inf 방어
    if np.any(np.isnan(yaw_rate)) or np.any(np.isinf(yaw_rate)):
        yaw_rate = np.zeros_like(yaw_rate)
    if np.any(np.isnan(yaw)) or np.any(np.isinf(yaw)):
        yaw = np.zeros_like(yaw)

    return yaw, yaw_rate