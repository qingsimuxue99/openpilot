#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
萝卜驾驶习惯自学习 (Carrot Driving-Habit Self-Learning)
========================================================

基于"奖励 / 惩罚"(强化学习思想) 的在线参数自适应守护进程。

核心思路
--------
在系统处于激活 (engaged) 状态时, 持续观察驾驶员的干预行为 (踩油门 / 踩刹车 /
接管) 以及系统自身的平顺性 (加速度), 把这些行为翻译成对每一个"可调舒适参数
(knob)"的奖励或惩罚信号; 再用"每个参数一个上下文老虎机 (contextual bandit)"
的方式, 把跟车距离 / 加速性 / 舒适制动 / 停车距离等参数, 一步一步地微调到贴合
你个人的驾驶习惯。

学习的行为语义 (奖励 / 惩罚)
----------------------------
* 跟车时你主动踩刹车            -> 车跟太近 / 刹得太晚   -> 加大跟车距离, 增强舒适制动
* 有前车但间距很大时你踩油门    -> 车跟太远 / 太肉       -> 减小跟车距离, 提高加速性
* 无前车 (或前车很远) 你踩油门  -> 起步 / 提速太肉       -> 提高对应速度段的加速性
* 系统自己猛刹 (你没踩)         -> 刹车太生硬            -> 降低舒适制动 (更柔和)
* 系统自己猛加 (你没踩)         -> 加速太冲              -> 降低对应速度段的加速性
* 停在前车后你点油门 (想更近)   -> 停车距离太远          -> 减小停车距离
* 低速接近前车你点刹车 (嫌近)   -> 停车距离太近          -> 加大停车距离
* 过弯中你踩刹车 (非跟车)       -> 进弯太快 / 降速不够    -> 增大过弯降速 (调小弯道横向G系数)
* 过弯中你踩油门 (非跟车)       -> 过弯降速太多 / 太肉    -> 减小过弯降速 (调大弯道横向G系数)
* 直道系统方向盘反复小摆(画龙)  -> 转向太灵敏 / 不平顺    -> 增大转向速率代价 (更柔和)
* 你同向扶盘帮转 (嫌转得慢)     -> 转向太肉 / 不跟手      -> 减小转向速率代价 (更灵敏)
* 踩刹车导致直接接管 (脱离)     -> 强烈不适 / 不安全      -> 大幅加大跟车距离 + 增强舒适制动
* 长时间平顺无干预              -> 当前参数贴合习惯      -> 收敛锁定 (减小探索步长)

关键设计: 上下文分桶 + 衰减自纠偏 + 安全边界
--------------------------------------------
v2 起, 每个旋钮的学习状态不再是"一条全局标量", 而是**按情境分桶的偏移字典**:
  * 弯道旋钮   -> 按横向加速度分 3 档 (缓弯/中弯/急弯), 各自独立学习。
  * 跟车旋钮   -> 按车速分 3 档 (低速/中速/高速), 各自独立学习。
  * 加速性     -> 本身已按速度段分成 7 个旋钮, 天然分桶, 无需再分。
  * 舒适制动/停车距离/转向手感 -> 无天然子情境, 用单一全局桶。
效果: 缓弯你常踩油 -> 只让缓弯不减速; 急弯你常踩刹 -> 只让急弯照减。
      这正是"该减速时减速, 不该减速时不减速"。

衰减自纠偏: 某个情境桶若很久没有新的干预信号, 其学到的偏移会逐步衰减回 0,
使参数自动回到基线。这样"偶尔冲一下"不会永久定格成"以后都不减速", 学歪了
会自动回正 —— 你既敢让它学, 又不怕它学飞。

安全边界: 每个旋钮有比 UI 更严格的安全区 (SAFE_BOUNDS), 学习永不允许把参数
推入危险/极端方向 (如弯道系数过高=不减速, 跟车过近, 加速过猛),   但不限制安全方向。

第一阶段优化(让学习更懂司机、避免学歪):
  * 安全边界附近降权 —— 把参数推向"危险边界"的接管视为安全纠偏而非偏好, 自动降权;
  * 偏离幅度加权 —— 与 OP 规划分歧越大(如 OP 还在加速你却踩刹)信号越强;
  * 跨参数可行性投影 —— 跟车距随档位非减 / 加速性随速度段非增, 杜绝矛盾组合;
  * 高峰/平峰情境 —— 工作日早晚高峰单独成桶, 并从平峰继承避免冷启动。

安全原则 (务必遵守)
-------------------
1. 只调节"驾驶员本来就能在 UI 手动调节的舒适性整数参数", 绝不触碰任何转向 /
   扭矩 / 安全关键的底层控制。
2. 每个参数都有比 UI 更严格的硬上下限 (HARD CLAMP) 与安全区 (SAFE_BOUNDS),
   越界一律截断, 且学习只能往"更安全"方向突破安全区边界, 不能往"更危险"方向。
3. 每个自适应周期 (约 45s) 每个参数每个情境桶最多只走 1 步 (step), 变化平缓可控。
4. 仅在 [功能开启] 且 [系统激活] 时才学习; 写参数与"用户手动改参数"走的是
   完全相同的既有管线 (Params), 不绕过任何现有安全逻辑。
5. 学到的值就是 UI 里能看到 / 能手动改 / 能一键重置的那些参数。
6. 一键重置 (CarrotLearningReset=1) 会把所有相关参数恢复到"开启学习前的基线值"。
7. 每次真正写入参数都会追加一条可读记录到 carrot_learn_changes.json (时间/参数/
   旧值->新值/原因/触发来源), 滚动保留最近 100 条, 满则自动删除最旧的。
8. 用户在学习开启后手动修改某参数 -> 以该新值为新的基线, 并清除该参数的学习偏移,
   避免学习器与手动设置互相打架。

作者: 清月 (WorkBuddy) — 为 carrot 0.9.9 (comma three) 定制
"""

import os
import json
import time

import cereal.messaging as messaging
from cereal import log
from openpilot.common.params import Params
from openpilot.common.realtime import Ratekeeper, DT_MDL
from openpilot.common.swaglog import cloudlog
from openpilot.selfdrive.carrot.config import UnifiedParams

CV_MS_TO_KPH = 3.6

# ----------------------------------------------------------------------------
# 可学习参数 (knob) 定义
#   lo / hi : 硬上下限 (比 UI 范围更保守, 单位与该参数原始整数一致)
#   step    : 每个自适应周期的最大调整步长
#   group   : 归属的行为分组
#   参数原始含义:
#     TFollowGapN   : 跟车时距, 值/100 = 秒
#     CruiseMaxValsN: 各速度段目标加速度, 值/100 = m/s^2
#     ComfortBrake  : 舒适制动减速度, 值/100 = m/s^2 (值越大刹得越果断/越早)
#     StopDistanceCarrot: 停车后与前车距离, 单位 cm
# ----------------------------------------------------------------------------
KNOB_SPECS = {
  # 跟车距离 (对应 4 档 personality: aggressive/standard/relaxed/moreRelaxed)
  "TFollowGap1": {"lo": 90,  "hi": 200, "step": 3,  "group": "gap"},
  "TFollowGap2": {"lo": 100, "hi": 230, "step": 3,  "group": "gap"},
  "TFollowGap3": {"lo": 120, "hi": 260, "step": 3,  "group": "gap"},
  "TFollowGap4": {"lo": 140, "hi": 300, "step": 3,  "group": "gap"},
  # 加速性 (7 个速度分段点)
  "CruiseMaxVals0": {"lo": 80, "hi": 220, "step": 4, "group": "accel"},
  "CruiseMaxVals1": {"lo": 80, "hi": 220, "step": 4, "group": "accel"},
  "CruiseMaxVals2": {"lo": 60, "hi": 180, "step": 4, "group": "accel"},
  "CruiseMaxVals3": {"lo": 50, "hi": 160, "step": 4, "group": "accel"},
  "CruiseMaxVals4": {"lo": 40, "hi": 140, "step": 3, "group": "accel"},
  "CruiseMaxVals5": {"lo": 35, "hi": 120, "step": 3, "group": "accel"},
  "CruiseMaxVals6": {"lo": 30, "hi": 110, "step": 3, "group": "accel"},
  # 舒适制动
  "ComfortBrake": {"lo": 180, "hi": 320, "step": 4, "group": "brake"},
  # 停车距离
  "StopDistanceCarrot": {"lo": 350, "hi": 850, "step": 10, "group": "stopdist"},
  # 弯道降速: 横向加速度舒适系数 (值越大 -> 允许更大过弯横向G -> 降速越少/过弯越快)
  #   分普通路 / 高速两档, 与 carrot_man 按 roadcate 选用两套系数对齐, 这里用车速近似区分
  "AutoCurveSpeedAggressiveness":  {"lo": 60, "hi": 200, "step": 3, "group": "curve"},
  "AutoCurveSpeedAggressivenessH": {"lo": 60, "hi": 200, "step": 3, "group": "curve"},
  # 转向松紧手感: 横向 MPC 转向速率代价 (无条件喂给 lat_mpc, 任何车道模式都生效)
  #   值越低 -> 转向越积极灵敏; 值越高 -> 转向越平顺柔和。默认 7, UI 原生范围 0~1000,
  #   这里收敛到安全舒适区间, 不走极端。
  "LatMpcSteeringRateCost": {"lo": 3, "hi": 40, "step": 1, "group": "steer"},
}

# 安全区 (学习永不允许把参数推入危险方向, 但允许往安全方向走到硬限):
#   * 跟车距离: 只封下界 (太近危险), 上界放开(远一点只是效率低)
#   * 加速性:   只封上界 (太冲危险), 下界放开(肉一点只是慢)
#   * 舒适制动: 只封上界 (太硬危险), 下界放开(柔一点只是晚)
#   * 停车距离: 只封下界 (太近危险), 上界放开
#   * 弯道系数: 只封上界 (太猛=不减速危险), 下界放开(更慢只是肉)
#   * 转向手感: 双向放开 (均在安全舒适区间)
SAFE_BOUNDS = {
  "TFollowGap1": (100, 200), "TFollowGap2": (110, 230), "TFollowGap3": (130, 260), "TFollowGap4": (150, 300),
  "CruiseMaxVals0": (80, 180), "CruiseMaxVals1": (80, 180), "CruiseMaxVals2": (60, 150),
  "CruiseMaxVals3": (50, 140), "CruiseMaxVals4": (40, 120), "CruiseMaxVals5": (35, 105), "CruiseMaxVals6": (30, 100),
  "ComfortBrake": (180, 280), "StopDistanceCarrot": (400, 850),
  "AutoCurveSpeedAggressiveness": (60, 160), "AutoCurveSpeedAggressivenessH": (60, 160),
  "LatMpcSteeringRateCost": (3, 40),
}
for _k, _v in SAFE_BOUNDS.items():
  if _k in KNOB_SPECS:
    KNOB_SPECS[_k]["safe_lo"], KNOB_SPECS[_k]["safe_hi"] = _v

# 每个旋钮的"危险边界"方向: 学习把参数推向该边界即视为潜在安全风险, 需降权
#   gap/stopdist: 太近危险 -> 下界(lo)是危险边界
#   accel/brake/curve: 太冲/太硬/不减速危险 -> 上界(hi)是危险边界
#   steer: 双向均安全 -> 无
DANGER_BOUND = {
  "TFollowGap1": "lo", "TFollowGap2": "lo", "TFollowGap3": "lo", "TFollowGap4": "lo",
  "CruiseMaxVals0": "hi", "CruiseMaxVals1": "hi", "CruiseMaxVals2": "hi", "CruiseMaxVals3": "hi",
  "CruiseMaxVals4": "hi", "CruiseMaxVals5": "hi", "CruiseMaxVals6": "hi",
  "ComfortBrake": "hi", "StopDistanceCarrot": "lo",
  "AutoCurveSpeedAggressiveness": "hi", "AutoCurveSpeedAggressivenessH": "hi",
  "LatMpcSteeringRateCost": None,
}
for _k, _v in DANGER_BOUND.items():
  if _k in KNOB_SPECS:
    KNOB_SPECS[_k]["danger_bound"] = _v

# 加速度速度分段点 (km/h), 与 carrot_functions.A_CRUISE_MAX_BP_CARROT 对齐
ACCEL_BP_KPH = [0.0, 10.0, 40.0, 60.0, 80.0, 110.0, 140.0]
ACCEL_KEYS = [f"CruiseMaxVals{i}" for i in range(7)]

# personality -> 当前生效的跟车距离参数
PERSONALITY_TO_GAP = {
  int(log.LongitudinalPersonality.aggressive):  "TFollowGap1",
  int(log.LongitudinalPersonality.standard):    "TFollowGap2",
  int(log.LongitudinalPersonality.relaxed):     "TFollowGap3",
  int(log.LongitudinalPersonality.moreRelaxed): "TFollowGap4",
}

# ------- 学习超参数 -------
ADAPT_INTERVAL_S = 45.0     # 每隔多少"激活秒"做一次自适应
EMA_ALPHA = 0.4             # 奖励 EMA 平滑系数
DEADBAND = 0.10            # 净奖励死区, 低于此值不动 (抗抖动; 分桶后每桶样本变稀, 故比 0.15 略放宽以免太难学)
MIN_SAMPLES = 3.0          # 一个周期内某情境桶至少要有多少加权样本才更新 (分桶后每桶样本变稀, 由 4 降到 3 以免少跑的桶永远学不动)
DECAY = 0.95               # 未达样本桶的 EMA/偏移衰减系数 (久无干预 -> 自纠偏回基线); 由0.9放慢到0.95让大偏移更"粘"
MIN_HOLD = 2               # 偏移绝对值<=此值时不衰减, 直接保留 (防 int(off*DECAY) 把 ±1/±2 小偏移瞬间截断清零)
HARSH_DECEL = -2.6         # 系统自身"猛刹"阈值 (m/s^2)
HARSH_ACCEL = 2.4          # 系统自身"猛加"阈值 (m/s^2)

# ------- 弯道学习超参数 -------
CURVE_LAT_A_MIN = 1.3      # 判定"正在过弯"的横向加速度下限 (m/s^2), = |yawRate|*vEgo
CURVE_MIN_VEGO = 3.0       # 过弯判定的最低车速 (m/s), 低于此不算过弯 (排除原地打方向)
HIGHWAY_KPH = 65.0         # 普通路/高速分档的车速近似阈值 (km/h)
CURVE_LEAD_MASK = 8.0      # 弯中踩刹车时, 若前车距离 < max(此值, vEgo*2.0) 视为跟车刹车, 不计入弯道
# 弯道分 3 档 (按横向加速度, m/s^2): 缓弯 / 中弯 / 急弯
CURVE_BAND_A = 2.2
CURVE_BAND_B = 3.2

# ------- 跟车学习超参数 (按车速分 3 档) -------
SPEED_BAND_A = 40.0        # 低速/中速 边界 (km/h)
SPEED_BAND_B = 90.0        # 中速/高速 边界

# ------- 转向松紧学习超参数 -------
STEER_MIN_VEGO = 8.0       # 低于此车速不做转向手感学习 (低速转向本就是大动作, 易误判)
STEER_STRAIGHT_YAW = 0.06  # 判定"直道"的横摆角速度上限 (rad/s)
STEER_RATE_DEADZONE = 10.0 # 方向盘角速度死区 (deg/s), 超过才计入"摆动"变号
STEER_OSC_WINDOW = 3.0     # 画龙统计窗口 (s)
STEER_OSC_TRIGGER = 6      # 窗口内方向盘变号次数达到此值 -> 判定画龙/太灵敏
STEER_HELP_MIN_ANGLE = 3.0 # 判定"用户帮转"的最小方向盘转角 (deg), 排除直道零位抖动
STEER_HELP_HOLDOFF = 4.0   # 转向信号冷却 (s), 防止连续重复计数

STATE_VERSION = 5

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carrot_learn_state.json")

# ---- 参数修改记录 (变更日志) ----
CHANGES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carrot_learn_changes.json")
MAX_CHANGE_LOG = 100       # 最多保留的学习记录条数, 满则自动丢弃最旧的

# 参数 -> 人类可读名称 (用于记录)
KNOB_LABELS = {
  "TFollowGap1": "跟车距离(激进档)",
  "TFollowGap2": "跟车距离(标准档)",
  "TFollowGap3": "跟车距离(宽松档)",
  "TFollowGap4": "跟车距离(更宽松档)",
  "CruiseMaxVals0": "加速性(0-10km/h)",
  "CruiseMaxVals1": "加速性(10-40km/h)",
  "CruiseMaxVals2": "加速性(40-60km/h)",
  "CruiseMaxVals3": "加速性(60-80km/h)",
  "CruiseMaxVals4": "加速性(80-110km/h)",
  "CruiseMaxVals5": "加速性(110-140km/h)",
  "CruiseMaxVals6": "加速性(140+km/h)",
  "ComfortBrake": "舒适制动",
  "StopDistanceCarrot": "停车距离",
  "AutoCurveSpeedAggressiveness": "弯道过弯系数(普通路)",
  "AutoCurveSpeedAggressivenessH": "弯道过弯系数(高速)",
  "LatMpcSteeringRateCost": "转向松紧手感",
}

# 每个分组: 参数 +1 / -1 方向的人类可读含义 (用于记录 "原因")
GROUP_DIR_REASON = {
  "gap":      {"+": "跟车距离加大(更远/更稳)",      "-": "跟车距离减小(更近/更跟)"},
  "accel":    {"+": "加速性提高(起步/提速更快)",    "-": "加速性降低(加速更平缓)"},
  "brake":    {"+": "舒适制动增强(刹得更早/果断)",  "-": "舒适制动减弱(刹得更柔和)"},
  "stopdist": {"+": "停车距离加大(停得更远)",       "-": "停车距离减小(停得更近)"},
  "curve":    {"+": "过弯降速减少(过弯更快)",       "-": "过弯降速增多(过弯更稳)"},
  "steer":    {"+": "转向更平顺柔和(代价增大)",     "-": "转向更灵敏跟手(代价减小)"},
}

# 情境桶 -> 可读标签 (附在记录原因后, 便于追溯是哪类场景学到的)
BAND_LABELS = {
  "g0": "(缓弯)", "g1": "(中弯)", "g2": "(急弯)",
  "s0": "(低速)", "s1": "(中速)", "s2": "(高速)",
}


def _clip(v, lo, hi):
  return max(lo, min(hi, v))


def _sign(x):
  return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)


def _curve_band(lat_accel):
  """横向加速度 -> 弯道情境桶 (g0 缓弯 / g1 中弯 / g2 急弯 / '' 非弯)。"""
  if lat_accel < CURVE_LAT_A_MIN:
    return ""
  if lat_accel < CURVE_BAND_A:
    return "g0"
  if lat_accel < CURVE_BAND_B:
    return "g1"
  return "g2"


def _speed_band(v_kph):
  """车速 -> 跟车情境桶 (s0 低速 / s1 中速 / s2 高速)。"""
  if v_kph < SPEED_BAND_A:
    return "s0"
  if v_kph < SPEED_BAND_B:
    return "s1"
  return "s2"


def _gap_ctx(v_kph, rush):
  """跟车情境桶 (s0 低速 / s1 中速 / s2 高速), 工作日高峰时段追加 'r' 后缀区分拥堵场景。"""
  return _speed_band(v_kph) + ("r" if rush else "")


def _band_label(ctx):
  return BAND_LABELS.get(ctx, "")


def _category_of(key):
  """把参数映射到 UI 分类标签(加速/跟车/转向/弯道)。"""
  if key in ACCEL_KEYS:
    return "accel"
  if key.startswith("TFollow") or key in ("ComfortBrake", "StopDistanceCarrot"):
    return "follow"
  if key == "LatMpcSteeringRateCost":
    return "steer"
  if key.startswith("AutoCurveSpeed"):
    return "curve"
  return "other"


class CarrotLearner:
  def __init__(self):
    self.params = Params()
    self.uparams = UnifiedParams()

    # 运行态
    self.enabled = False
    self.prev_enabled_param = False
    self.engaged = False
    self.prev_engaged = False
    self.prev_gas = False
    self.prev_brake = False

    self.engaged_time = 0.0          # 累计激活时长 (秒), 触发自适应用
    self.session_engaged_time = 0.0  # 本次上电累计激活时长
    self.time_since_adapt = 0.0
    self.harsh_decel_holdoff = 0.0
    self.harsh_accel_holdoff = 0.0

    # 每周期奖励累加器: knob -> {ctx: [reward_sum, sample_weight]}
    self.acc = {k: {} for k in KNOB_SPECS}

    # 弯道学习开关 (子开关, 默认开)
    self.curve_enabled = True

    # 转向松紧学习开关 (子开关, 默认开) 与画龙检测状态
    self.steer_enabled = True
    self.prev_osc_sign = 0.0      # 上次方向盘角速度符号 (画龙变号检测)
    self.osc_count = 0            # 当前窗口内的变号次数
    self.osc_window_t = 0.0       # 画龙统计窗口计时 (s)
    self.osc_holdoff = 0.0        # 画龙信号冷却
    self.steer_help_holdoff = 0.0 # 帮转信号冷却

    # 干预统计 (用于状态显示)
    self.brake_overrides = 0
    self.gas_overrides = 0
    self.curve_overrides = 0
    self.steer_overrides = 0
    self.adjust_count = 0

    # 稳定度 (近期干预越少 -> 越稳定 -> 探索步长越小)
    self.instability = 1.0

    # 持久化学习状态 (v2: 按情境桶的 EMA 与偏移字典)
    self.ema = {k: {} for k in KNOB_SPECS}      # knob -> {ctx: 净奖励EMA}
    self.offset = {k: {} for k in KNOB_SPECS}    # knob -> {ctx: 相对基线整数偏移}
    self.baseline = {}
    # 当前各旋钮激活情境 (用于上下文切换时重写参数) 与上次写入值 (用于检测手动修改)
    self.last_ctx = {k: "" for k in KNOB_SPECS}
    self.last_written = {}
    # 参数修改记录 (最新的在末尾), 最多 MAX_CHANGE_LOG 条
    self.change_log = []
    self.change_seq = 0
    self._load_state()
    self._load_changes()

  # ----------------------------------------------------------------- 持久化
  def _load_state(self):
    try:
      if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
          data = json.load(f)
        if data.get("version") == STATE_VERSION:
          self.baseline = data.get("baseline", {})
          self.ema = {k: {} for k in KNOB_SPECS}
          self.offset = {k: {} for k in KNOB_SPECS}
          for k, bins in data.get("ema", {}).items():
            if k in self.ema and isinstance(bins, dict):
              self.ema[k] = {c: float(v) for c, v in bins.items()}
          for k, bins in data.get("offset", {}).items():
            if k in self.offset and isinstance(bins, dict):
              self.offset[k] = {c: int(v) for c, v in bins.items()}
          self.brake_overrides = int(data.get("brake_overrides", 0))
          self.gas_overrides = int(data.get("gas_overrides", 0))
          self.curve_overrides = int(data.get("curve_overrides", 0))
          self.steer_overrides = int(data.get("steer_overrides", 0))
          self.adjust_count = int(data.get("adjust_count", 0))
          self.instability = float(data.get("instability", 1.0))
          # 重启后保持"上次学习器写入值", 使手动修改检测跨重启有效
          # (用户手动改过 -> 重启后不会被学习器覆盖回基线/默认)
          for k, v in data.get("last_written", {}).items():
            if k in KNOB_SPECS:
              self.last_written[k] = int(v)
    except Exception as e:
      cloudlog.exception(f"carrot_learner: load state failed: {e}")

  def _save_state(self):
    try:
      data = {
        "version": STATE_VERSION,
        "ema": self.ema,
        "offset": self.offset,
        "baseline": self.baseline,
        "brake_overrides": self.brake_overrides,
        "gas_overrides": self.gas_overrides,
        "curve_overrides": self.curve_overrides,
        "steer_overrides": self.steer_overrides,
        "adjust_count": self.adjust_count,
        "instability": round(self.instability, 4),
        "last_written": {k: int(v) for k, v in self.last_written.items()},
        "updated_at": int(time.time()),
      }
      tmp = STATE_PATH + ".tmp"
      with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
      os.replace(tmp, STATE_PATH)
    except Exception as e:
      cloudlog.exception(f"carrot_learner: save state failed: {e}")

  # ---------------------------------------------------- 参数修改记录 (变更日志)
  def _load_changes(self):
    """载入已有的参数修改记录 (滚动日志)。"""
    try:
      if os.path.exists(CHANGES_PATH):
        with open(CHANGES_PATH, "r", encoding="utf-8") as f:
          data = json.load(f)
        self.change_log = data.get("changes", []) or []
        self.change_seq = int(data.get("seq", len(self.change_log)))
        # 防御: 载入后也强制不超过上限
        if len(self.change_log) > MAX_CHANGE_LOG:
          self.change_log = self.change_log[-MAX_CHANGE_LOG:]
    except Exception as e:
      cloudlog.exception(f"carrot_learner: load changes failed: {e}")
      self.change_log = []

  def _save_changes(self):
    """把参数修改记录写盘 (原子替换)。"""
    try:
      data = {
        "version": STATE_VERSION,
        "max_records": MAX_CHANGE_LOG,
        "seq": self.change_seq,
        "count": len(self.change_log),
        "updated_at": int(time.time()),
        "changes": self.change_log,
      }
      tmp = CHANGES_PATH + ".tmp"
      with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
      os.replace(tmp, CHANGES_PATH)
    except Exception as e:
      cloudlog.exception(f"carrot_learner: save changes failed: {e}")

  def _reason_for(self, key, delta):
    """根据参数分组与调整方向, 生成人类可读的修改原因。"""
    spec = KNOB_SPECS.get(key, {})
    group = spec.get("group", "")
    d = "+" if delta > 0 else "-"
    return GROUP_DIR_REASON.get(group, {}).get(d, "参数微调")

  def _log_change(self, key, old, new, trigger="", ctx=""):
    """追加一条参数修改记录; 满 MAX_CHANGE_LOG 条时自动删除最旧的。"""
    if new == old:
      return
    self.change_seq += 1
    now = time.time()
    try:
      ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    except Exception:
      ts = ""
    reason = self._reason_for(key, new - old)
    if ctx:
      reason = reason + " " + _band_label(ctx)
    entry = {
      "seq": self.change_seq,               # 递增序号 (删除旧记录也不回退)
      "time": ts,                           # 本地时间字符串
      "epoch": int(now),                    # Unix 时间戳
      "param": key,                         # 参数名 (与 UI 一致)
      "label": KNOB_LABELS.get(key, key),   # 中文名称
      "old": old,                           # 旧值 (整数原始值)
      "new": new,                           # 新值
      "delta": new - old,                   # 变化量
      "reason": reason,                     # 修改含义 (含情境桶标签)
      "trigger": trigger,                   # 触发来源: 学习/脱离/重置/手动修改重置基线
      "ctx": ctx,                           # 情境桶 (空=全局)
    }
    self.change_log.append(entry)
    # 满 100 条 -> 丢弃最旧的, 只保留最近 MAX_CHANGE_LOG 条
    if len(self.change_log) > MAX_CHANGE_LOG:
      self.change_log = self.change_log[-MAX_CHANGE_LOG:]
    self._save_changes()

  # ------------------------------------------------------------ 基线 / 重置
  def _capture_baseline(self):
    """记录开启学习前的参数值, 供一键重置恢复。"""
    self.baseline = {}
    for k in KNOB_SPECS:
      self.baseline[k] = self._get_knob(k)
    self._save_state()
    cloudlog.info(f"carrot_learner: baseline captured: {self.baseline}")

  def _do_reset(self):
    """恢复到基线并清空学习状态。"""
    restored = 0
    if self.baseline:
      for k, v in self.baseline.items():
        if k in KNOB_SPECS:
          spec = KNOB_SPECS[k]
          cur = self._get_knob(k)
          base_val = int(_clip(v, spec["lo"], spec["hi"]))
          self.params.put_int(k, base_val)
          if base_val != cur:
            restored += 1
    self.ema = {k: {} for k in KNOB_SPECS}
    self.offset = {k: {} for k in KNOB_SPECS}
    self.acc = {k: {} for k in KNOB_SPECS}
    self.brake_overrides = 0
    self.gas_overrides = 0
    self.curve_overrides = 0
    self.steer_overrides = 0
    self.adjust_count = 0
    self.instability = 1.0
    self.engaged_time = 0.0
    self.time_since_adapt = 0.0
    self.prev_osc_sign = 0.0
    self.osc_count = 0
    self.osc_window_t = 0.0
    self.last_ctx = {k: "" for k in KNOB_SPECS}
    self.last_written = {}
    self._save_state()
    # 记录一条重置标记 (可追溯何时做过一键重置, 恢复了多少项)
    self.change_seq += 1
    try:
      ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    except Exception:
      ts = ""
    self.change_log.append({
      "seq": self.change_seq, "time": ts, "epoch": int(time.time()),
      "param": "-", "label": "一键重置", "old": "-", "new": "-", "delta": 0,
      "reason": f"恢复到学习前基线, 共复位 {restored} 项参数", "trigger": "重置", "ctx": "",
    })
    if len(self.change_log) > MAX_CHANGE_LOG:
      self.change_log = self.change_log[-MAX_CHANGE_LOG:]
    self._save_changes()
    self.params.put("CarrotLearningStatus", "已重置到学习前基线")
    self.params.put_int("CarrotLearningReset", 0)
    cloudlog.info("carrot_learner: reset to baseline done")

  # ------------------------------------------------------------- 参数读写
  def _get_knob(self, key):
    try:
      return int(self.params.get_int(key))
    except Exception:
      return 0

  def _is_rush_hour(self):
    """工作日早晚高峰判定(本地时区)。周末不视为高峰。"""
    try:
      lt = time.localtime()
      if lt.tm_wday >= 5:   # 5=周六, 6=周日
        return False
      h = lt.tm_hour + lt.tm_min / 60.0
      return (7.0 <= h <= 9.5) or (17.0 <= h <= 19.5)
    except Exception:
      return False

  def _safety_weight(self, key, reward):
    """安全边界附近降权: 若本次奖惩方向把参数推向"危险边界"且该参数已逼近该边界,
    多半是安全纠偏而非偏好, 降权以免学到更危险的设定(如跟车更近/加速更猛/刹车更硬)。"""
    spec = KNOB_SPECS.get(key)
    if not spec:
      return 1.0
    danger = spec.get("danger_bound")
    if not danger:
      return 1.0
    cv = self._get_knob(key)
    lo, hi = spec["safe_lo"], spec["safe_hi"]
    if hi <= lo:
      return 1.0
    frac = (cv - lo) / (hi - lo)                       # 当前值在安全区内的位置 0..1
    toward_danger = (danger == "lo" and reward < 0) or (danger == "hi" and reward > 0)
    if not toward_danger:
      return 1.0
    prox = (1.0 - frac) if danger == "lo" else frac   # 0..1, 越大越逼近危险边界
    if prox >= 0.8:
      return 0.25
    if prox >= 0.6:
      return 0.5
    return 1.0

  def _add_reward(self, key, reward, weight=1.0, ctx=""):
    """累加某旋钮某情境桶的奖励。ctx 为空字符串表示全局桶。
    接入两项增强:
      * 安全边界附近降权 (_safety_weight): 推向危险边界的接管降权;
      * 高峰情境冷启动继承: 高峰桶(r 后缀)首次出现时从平峰对应桶继承, 避免从零学起。"""
    if key in self.acc:
      # 高峰情境首次出现, 从平峰对应桶继承 EMA/偏移, 避免冷启动
      if ctx.endswith("r") and ctx not in self.ema[key] and ctx[:-1] in self.ema[key]:
        self.ema[key][ctx] = self.ema[key][ctx[:-1]]
        self.offset[key][ctx] = self.offset[key].get(ctx[:-1], 0)
      w = weight * self._safety_weight(key, reward)
      self.acc[key].setdefault(ctx, [0.0, 0.0])
      self.acc[key][ctx][0] += reward * w
      self.acc[key][ctx][1] += w

  # ---------------------------------------------------------- 奖励 / 惩罚
  def _observe(self, sm):
    """每帧观察驾驶行为, 累加奖励/惩罚信号 (仅在激活时调用)。"""
    cs = sm['carState']
    v_ego = float(cs.vEgo)
    a_ego = float(cs.aEgo)
    v_kph = v_ego * CV_MS_TO_KPH
    gas = bool(cs.gasPressed)
    brake = bool(cs.brakePressed)
    try:
      yaw_rate = float(cs.yawRate)
    except Exception:
      yaw_rate = 0.0
    # 实测横向加速度估计 (m/s^2) = |横摆角速度| * 车速
    lat_accel = abs(yaw_rate) * v_ego

    # 方向盘信号 (用于转向松紧学习; 与踏板信号相互独立)
    try:
      steer_rate = float(cs.steeringRateDeg)
      steer_angle = float(cs.steeringAngleDeg)
      steer_torque = float(cs.steeringTorque)
      steer_pressed = bool(cs.steeringPressed)
      blinker = bool(cs.leftBlinker) or bool(cs.rightBlinker)
    except Exception:
      steer_rate = steer_angle = steer_torque = 0.0
      steer_pressed = blinker = False

    # 前车信息
    lead_present = False
    d_rel = 999.0
    v_rel = 0.0
    try:
      lead = sm['radarState'].leadOne
      lead_present = bool(lead.status)
      d_rel = float(lead.dRel)
      v_rel = float(lead.vRel)
    except Exception:
      pass

    # 当前生效的跟车档 (由 personality 决定)
    try:
      personality = self.params.get_int("LongitudinalPersonality")
    except Exception:
      personality = int(log.LongitudinalPersonality.standard)
    gap_key = PERSONALITY_TO_GAP.get(personality, "TFollowGap2")

    # 当前速度对应的加速度分段 (取最近的 1~2 个分段点, 按插值权重分摊)
    accel_targets = self._accel_context(v_kph)

    # 情境桶: 弯道按横向加速度, 跟车按车速(含高峰标记), 其余全局
    curve_ctx = _curve_band(lat_accel)
    rush = self._is_rush_hour()
    gap_ctx = _gap_ctx(v_kph, rush)

    gas_edge = gas and not self.prev_gas
    brake_edge = brake and not self.prev_brake

    # ---- 1) 跟车中主动踩刹车: 跟太近 / 刹太晚 ----
    if brake_edge and lead_present and d_rel < max(8.0, v_ego * 2.5):
      self.brake_overrides += 1
      self.instability = min(1.5, self.instability + 0.25)
      # 收得越紧 / 逼近越快, 惩罚越强
      severity = 1.0
      if v_rel < -1.0:
        severity += min(1.5, -v_rel * 0.3)
      if d_rel < v_ego * 1.5:
        severity += 0.5
      # 偏离幅度加权: OP 当时若仍在加速(a_ego>0)而司机踩刹, 说明与规划分歧大 -> 信号更强
      disc = 1.0 + min(1.0, max(0.0, a_ego) * 0.4)
      self._add_reward(gap_key, +1.0, severity * disc, gap_ctx)   # 加大跟车距离
      self._add_reward("ComfortBrake", +1.0, 0.6 * severity)  # 更早/更果断地刹(近危险边界由 _safety_weight 降权)

    # ---- 2) 无(远)前车时踩刹车: 系统减速不够主动 (弱信号) ----
    elif brake_edge and (not lead_present or d_rel > 60.0):
      self._add_reward("ComfortBrake", +1.0, 0.25)

    # ---- 3) 踩油门 (engaged): 太肉 ----
    if gas_edge:
      self.gas_overrides += 1
      self.instability = min(1.5, self.instability + 0.2)
      # 偏离幅度加权: OP 当时仍在减速(a_ego<0)而司机踩油, 分歧大 -> 信号更强
      disc = 1.0 + min(1.0, max(0.0, -a_ego) * 0.4)
      if lead_present and d_rel > max(12.0, v_ego * 2.0):
        # 有前车但间距很大 -> 跟太远 + 提速太肉
        self._add_reward(gap_key, -1.0, 0.8 * disc, gap_ctx)  # 减小跟车距离
        for k, w in accel_targets:
          self._add_reward(k, +1.0, 0.8 * w * disc)          # 提高加速性
      else:
        # 无前车/前车很近但你想更快 -> 提速太肉
        for k, w in accel_targets:
          self._add_reward(k, +1.0, 1.0 * w * disc)

    # ---- 4) 系统自身猛刹 (你没踩刹车): 刹车太生硬 ----
    if self.harsh_decel_holdoff <= 0.0 and not brake and a_ego < HARSH_DECEL:
      self._add_reward("ComfortBrake", -1.0, 0.7)            # 刹得更柔和
      self.harsh_decel_holdoff = 3.0

    # ---- 5) 系统自身猛加 (你没踩油门): 加速太冲 ----
    if self.harsh_accel_holdoff <= 0.0 and not gas and a_ego > HARSH_ACCEL:
      for k, w in accel_targets:
        self._add_reward(k, -1.0, 0.6 * w)                  # 降低该速度段加速性
      self.harsh_accel_holdoff = 3.0

    # ---- 6) 停车距离 (低速跟停场景) ----
    if lead_present and v_ego < 3.0:
      if gas_edge:
        # 停/爬行时点油门 -> 想更近 -> 减小停车距离
        self._add_reward("StopDistanceCarrot", -1.0, 0.8)
      elif brake_edge and (v_rel < -0.2 or d_rel < 6.0):
        # 低速接近时点刹车 -> 嫌近 -> 加大停车距离
        self._add_reward("StopDistanceCarrot", +1.0, 0.8)

    # ---- 7) 弯道降速自学习 (按曲率分桶) ----
    #   Aggressiveness 越大 -> 目标横向G越大 -> 过弯降速越少(过弯越快)。
    if self.curve_enabled and curve_ctx:
      curve_key = "AutoCurveSpeedAggressivenessH" if v_kph >= HIGHWAY_KPH else "AutoCurveSpeedAggressiveness"
      # 弯中前车是否很近 (若很近, 刹车更可能是跟车而非嫌弯道快, 排除以免重复归因)
      lead_close = lead_present and d_rel < max(CURVE_LEAD_MASK, v_ego * 2.0)
      if brake_edge and not lead_close:
        # 弯中主动踩刹车 -> 系统进弯太快 -> 调小 Aggressiveness (增大过弯降速)
        self.curve_overrides += 1
        self.instability = min(1.5, self.instability + 0.2)
        severity = 1.0 + min(1.5, max(0.0, lat_accel - 1.6))  # 横向G越大, 越不适, 惩罚越强
        self._add_reward(curve_key, -1.0, severity, curve_ctx)
      elif gas_edge and not lead_close:
        # 弯中踩油门 -> 系统降速太多太肉 -> 调大 Aggressiveness (减小过弯降速)
        self.curve_overrides += 1
        self.instability = min(1.5, self.instability + 0.15)
        self._add_reward(curve_key, +1.0, 0.9, curve_ctx)

    # ---- 8) 转向松紧手感自学习 (LatMpcSteeringRateCost) ----
    #   信号源全部来自方向盘 (角速度 / 扭矩 / 施力), 与踏板信号天然分离, 不与纵向/弯道
    #   学习重复归因。cost 越低 -> 转向越积极灵敏; 越高 -> 越平顺柔和。
    if self.steer_enabled and v_ego > STEER_MIN_VEGO:
      # (A) 直道 + 无人干预时, 方向盘反复小摆 (画龙) -> 太灵敏 -> 增大 cost (更平顺)
      if abs(yaw_rate) < STEER_STRAIGHT_YAW and not steer_pressed:
        self.osc_window_t += DT_MDL
        if abs(steer_rate) > STEER_RATE_DEADZONE:
          cur_sign = _sign(steer_rate)
          if self.prev_osc_sign != 0.0 and cur_sign != self.prev_osc_sign:
            self.osc_count += 1
          self.prev_osc_sign = cur_sign
        if self.osc_window_t >= STEER_OSC_WINDOW:
          if self.osc_holdoff <= 0.0 and self.osc_count >= STEER_OSC_TRIGGER:
            self.steer_overrides += 1
            self.instability = min(1.5, self.instability + 0.15)
            severity = 1.0 + min(1.0, (self.osc_count - STEER_OSC_TRIGGER) * 0.2)
            self._add_reward("LatMpcSteeringRateCost", +1.0, severity)  # 更平顺
            self.osc_holdoff = STEER_HELP_HOLDOFF
          self.osc_window_t = 0.0
          self.osc_count = 0
          self.prev_osc_sign = 0.0
      else:
        # 进入弯道 / 用户接管 -> 重置画龙窗口, 避免跨状态误计
        self.osc_window_t = 0.0
        self.osc_count = 0
        self.prev_osc_sign = 0.0

      # (B) 用户同向扶盘帮转 (嫌系统转得慢/肉) -> 减小 cost (更灵敏)
      #   用扭矩符号判方向 (符号可靠), 用 steeringPressed 判是否在施力 (避开扭矩量纲不统一)
      if (steer_pressed and not blinker and self.steer_help_holdoff <= 0.0
          and abs(steer_angle) > STEER_HELP_MIN_ANGLE
          and steer_torque != 0.0 and _sign(steer_torque) == _sign(steer_angle)):
        self.steer_overrides += 1
        self.instability = min(1.5, self.instability + 0.15)
        self._add_reward("LatMpcSteeringRateCost", -1.0, 0.8)  # 更灵敏
        self.steer_help_holdoff = STEER_HELP_HOLDOFF

    self.prev_gas = gas
    self.prev_brake = brake

    # 上下文切换检测: 若任一旋钮激活情境变化, 立即重写参数 (保证当前弯/速档用对应偏移)
    ctx_map = {}
    for k, spec in KNOB_SPECS.items():
      g = spec["group"]
      if g == "curve":
        ctx_map[k] = curve_ctx
      elif g == "gap":
        ctx_map[k] = gap_ctx
      else:
        ctx_map[k] = ""
    if any(ctx_map.get(k) != self.last_ctx.get(k) for k in KNOB_SPECS):
      self._apply(ctx_map)
      self.last_ctx = ctx_map

  def _accel_context(self, v_kph):
    """把当前速度映射到最近的 1~2 个加速度分段, 返回 [(key, weight), ...]。"""
    v = _clip(v_kph, ACCEL_BP_KPH[0], ACCEL_BP_KPH[-1])
    # 找到相邻分段点做线性插值
    for i in range(len(ACCEL_BP_KPH) - 1):
      lo_bp, hi_bp = ACCEL_BP_KPH[i], ACCEL_BP_KPH[i + 1]
      if lo_bp <= v <= hi_bp:
        span = hi_bp - lo_bp
        if span <= 0:
          return [(ACCEL_KEYS[i], 1.0)]
        w_hi = (v - lo_bp) / span
        w_lo = 1.0 - w_hi
        out = []
        if w_lo > 0.05:
          out.append((ACCEL_KEYS[i], w_lo))
        if w_hi > 0.05:
          out.append((ACCEL_KEYS[i + 1], w_hi))
        return out or [(ACCEL_KEYS[i], 1.0)]
    return [(ACCEL_KEYS[-1], 1.0)]

  def _on_disengage(self, sm):
    """脱离瞬间 (engaged 1->0) 的强信号处理。"""
    cs = sm['carState']
    if bool(cs.brakePressed):
      # 踩刹车直接脱离: 强烈不适/不安全
      try:
        personality = self.params.get_int("LongitudinalPersonality")
      except Exception:
        personality = int(log.LongitudinalPersonality.standard)
      gap_key = PERSONALITY_TO_GAP.get(personality, "TFollowGap2")
      # 脱离时通常用当前车速分桶
      try:
        v_kph = float(cs.vEgo) * CV_MS_TO_KPH
      except Exception:
        v_kph = 0.0
      rush = self._is_rush_hour()
      gap_ctx = _gap_ctx(v_kph, rush)
      self._add_reward(gap_key, +1.0, 2.0, gap_ctx)
      self._add_reward("ComfortBrake", +1.0, 1.0)
      self.brake_overrides += 1
      self.instability = min(1.6, self.instability + 0.4)
      # 脱离是强事件, 立刻结算一次
      self._adapt(trigger="脱离接管")

  # ------------------------------------------------------------- 参数应用
  def _apply(self, ctx_map):
    """根据各旋钮当前激活情境桶, 把 基线+偏移 合成有效值写入 Param (并钳制到安全区)。
    同时检测用户手动修改: 若发现参数被外部改动, 以新值为基线并清除该参数偏移。"""
    for k, spec in KNOB_SPECS.items():
      ctx = ctx_map.get(k, "")
      base = self.baseline.get(k, self._get_knob(k))
      off = self.offset[k].get(ctx, 0)
      eff = int(_clip(base + off, spec["safe_lo"], spec["safe_hi"]))
      cur = self._get_knob(k)
      # 手动修改检测: 上次是我们写的 last_written, 若现在既不等于上次写入也不等于本次目标, 视为外部改
      if k in self.last_written and cur != self.last_written[k] and cur != eff:
        # 用户手动改了 -> 以新值为基线, 清除该参数所有情境偏移, 避免打架
        self.baseline[k] = cur
        self.offset[k] = {}
        self.ema[k] = {}
        self.last_written[k] = cur
        self._log_change(k, cur, cur, "手动修改重置基线", ctx=ctx)
        continue
      if eff != cur:
        self.params.put_int(k, eff)
        self.last_written[k] = eff
      else:
        self.last_written[k] = cur

  # ------------------------------------------------------------- 跨参数可行性投影
  def _project_feasible(self):
    """把学到的偏移投影到合理流形, 防止矛盾组合(仅在安全区内调整, 不引入新学习步长):
       * 跟车距离: 同情境下 aggressive<=standard<=relaxed<=moreRelaxed (随档位非减);
       * 加速性:   全局随速度段非增 (高速段目标加速度不应高于低速段)。
    投影后写回偏移; 若某参数为 0 偏移则清理该桶。"""
    # ---- 跟车距离: 各情境桶内 4 档单调非减 ----
    gap_keys = ["TFollowGap1", "TFollowGap2", "TFollowGap3", "TFollowGap4"]
    gap_ctxs = set()
    for k in gap_keys:
      gap_ctxs |= set(self.offset[k].keys())
    for ctx in gap_ctxs:
      eff = {}
      for k in gap_keys:
        spec = KNOB_SPECS[k]
        base = self.baseline.get(k, self._get_knob(k))
        off = self.offset[k].get(ctx, 0)
        eff[k] = int(_clip(base + off, spec["safe_lo"], spec["safe_hi"]))
      for i in range(1, len(gap_keys)):
        if eff[gap_keys[i]] < eff[gap_keys[i - 1]]:
          eff[gap_keys[i]] = eff[gap_keys[i - 1]]
      for k in gap_keys:
        spec = KNOB_SPECS[k]
        base = self.baseline.get(k, self._get_knob(k))
        new_off = int(_clip(eff[k] - base, spec["safe_lo"] - base, spec["safe_hi"] - base))
        if new_off == 0:
          self.offset[k].pop(ctx, None)
        else:
          self.offset[k][ctx] = new_off
    # ---- 加速性: 全局 7 段单调非增 ----
    eff = {}
    for k in ACCEL_KEYS:
      spec = KNOB_SPECS[k]
      base = self.baseline.get(k, self._get_knob(k))
      off = self.offset[k].get("", 0)
      eff[k] = int(_clip(base + off, spec["safe_lo"], spec["safe_hi"]))
    for i in range(1, len(ACCEL_KEYS)):
      if eff[ACCEL_KEYS[i]] > eff[ACCEL_KEYS[i - 1]]:
        eff[ACCEL_KEYS[i]] = eff[ACCEL_KEYS[i - 1]]
    for k in ACCEL_KEYS:
      spec = KNOB_SPECS[k]
      base = self.baseline.get(k, self._get_knob(k))
      new_off = int(_clip(eff[k] - base, spec["safe_lo"] - base, spec["safe_hi"] - base))
      if new_off == 0:
        self.offset[k].pop("", None)
      else:
        self.offset[k][""] = new_off

  # ------------------------------------------------------------- 自适应
  def _adapt(self, trigger="学习"):
    """把本周期累积的奖励折算进各情境桶 EMA, 对每个桶最多走一步; 未达样本桶衰减(自纠偏)。"""
    try:
      rate_param = self.params.get_int("CarrotLearningRate")
    except Exception:
      rate_param = 5
    rate_param = _clip(rate_param, 1, 10)

    # 稳定度衰减: 近期干预少 -> instability 趋近下限 -> 探索步长变小 (收敛锁定)
    self.instability = max(0.35, self.instability * 0.9)
    learn_scale = (rate_param / 5.0) * self.instability

    changed = 0
    for k, spec in KNOB_SPECS.items():
      base = self.baseline.get(k, self._get_knob(k))
      safe_lo = spec["safe_lo"]
      safe_hi = spec["safe_hi"]
      off_min = safe_lo - base
      off_max = safe_hi - base
      acc_k = self.acc.get(k, {})

      # 1) 遍历所有已知情境桶 (本周期有样本 / 历史有 EMA 或偏移), 更新或衰减
      #    注意: 必须覆盖"未在本次累加器中"的历史桶, 否则它们的偏移永远不会回退。
      all_ctx = set(acc_k.keys()) | set(self.ema[k].keys()) | set(self.offset[k].keys())
      reinforced = set()
      for ctx in all_ctx:
        rsum, w = acc_k.get(ctx, [0.0, 0.0])
        if w >= MIN_SAMPLES:
          avg = rsum / w
          self.ema[k][ctx] = EMA_ALPHA * avg + (1.0 - EMA_ALPHA) * self.ema[k].get(ctx, 0.0)
          reinforced.add(ctx)
        else:
          # 样本不足 / 无样本: 该桶 EMA 衰减 (久无干预 -> 净奖励记忆淡出)
          self.ema[k][ctx] = self.ema[k].get(ctx, 0.0) * DECAY
          # 同时让已学到的偏移逐步回退基线 (自纠偏核心)
          off = self.offset[k].get(ctx, 0)
          if off != 0:
            if abs(off) <= MIN_HOLD:
              # 小偏移保护: 已接近基线, 不再衰减, 避免 int(off*DECAY) 把 ±1/±2 瞬间截断归零
              # (此前"学到11步后全空"的根因之一; 保留已学偏好使其粘住, 下次在该情境行驶会重新评估)
              pass
            else:
              # 向 0 截断 (int 朝零取整), 大偏移逐步回退基线, 不会卡在边界
              new_off = int(off * DECAY)
              if new_off == 0:
                self.offset[k].pop(ctx, None)
              else:
                self.offset[k][ctx] = new_off
      self.acc[k] = {}

      # 2) 仅对"本次强化桶"结算: 净奖励超死区则走一步 (钳制到安全区相对边界)。
      #    只在强化桶上走步, 避免历史桶的残留 EMA 在无关周期里被反复误步进。
      for ctx in reinforced:
        r = self.ema[k][ctx]
        if abs(r) > DEADBAND:
          off = self.offset[k].get(ctx, 0)
          raw = _sign(r) * spec["step"] * learn_scale
          # 至少走 1 个整数, 避免因四舍五入卡死
          delta = int(_sign(raw) * max(1, round(abs(raw))))
          new_off = int(_clip(off + delta, off_min, off_max))
          if new_off != off:
            self.offset[k][ctx] = new_off
            self.adjust_count += 1
            changed += 1
            old_eff = int(_clip(base + off, safe_lo, safe_hi))
            new_eff = int(_clip(base + new_off, safe_lo, safe_hi))
            # 记录这次参数修改 (滚动保留最近 100 条)
            self._log_change(k, old_eff, new_eff, trigger, ctx=ctx)
            # 走了一步后衰减该方向的 EMA, 防止一路冲到底
            self.ema[k][ctx] *= 0.5
            cloudlog.info(f"carrot_learner: {k}[{ctx}] {old_eff} -> {new_eff} (r_ema={r:.2f}, scale={learn_scale:.2f})")
      # 3) 清理已无意义的历史桶 (EMA 趋零且无偏移)
      for ctx in list(self.ema[k].keys()):
        if ctx not in reinforced and ctx not in self.offset[k] and abs(self.ema[k][ctx]) < 1e-4:
          self.ema[k].pop(ctx, None)

    # 3) 跨参数可行性投影: 消除矛盾组合(如 aggressive 档跟车距反而大于 relaxed 档)
    self._project_feasible()
    # 4) 把当前情境的有效值写回 Param (审查模式下仅生成建议, 不自动写入)
    review_mode = self.params.get_int("CarrotLearningReview") == 1
    if not review_mode:
      self._apply(self.last_ctx)
    self._write_recommendations(applied=not review_mode)
    self._update_status(changed)
    self._save_state()

  def _update_status(self, changed_now=0):
    mins = int(self.session_engaged_time / 60.0)
    status = (f"学习中 | 本次{mins}分钟 | 刹车{self.brake_overrides} 油门{self.gas_overrides} "
              f"弯道{self.curve_overrides} 转向{self.steer_overrides} | 已生效{sum(len(v) for v in self.offset.values())}项(累计调整{self.adjust_count}次) "
              f"| 稳定度{max(0.0, 1.0 - (self.instability - 0.35)):.0%}")
    try:
      self.params.put("CarrotLearningStatus", status)
    except Exception:
      pass

  # ------------------------------------------------------------- 建议清单输出
  def _write_recommendations(self, applied):
    """输出学习建议清单供 UI 审阅(参考竞品"自动调参"风格, 差异化实现)。
    applied=True: 已由学习器自动写入(审查模式关闭); False: 仅建议待用户在弹窗确认。
    保留已有条目的 applied 标记, 避免学习器重算时覆盖用户在 UI 上的确认状态。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carrot_learn_recommendations.json")
    prev = {}
    try:
      if os.path.exists(path):
        with open(path, "r") as f:
          arr = json.load(f)
        for it in arr:
          prev[(it["key"], it.get("context", ""), it["new_value"])] = it.get("applied", False)
    except Exception:
      pass

    recs = []
    for k, spec in KNOB_SPECS.items():
      base = self.baseline.get(k, self._get_knob(k))
      cat = _category_of(k)
      for ctx in list(self.offset[k].keys()):
        off = self.offset[k][ctx]
        if off == 0:
          continue
        new_eff = int(_clip(base + off, spec["safe_lo"], spec["safe_hi"]))
        old_eff = int(_clip(base, spec["safe_lo"], spec["safe_hi"]))
        if new_eff == old_eff:
          continue
        reason = self._reason_for(k, off)
        if ctx:
          reason = reason + " " + _band_label(ctx)
        ctx_label = _band_label(ctx) if ctx else "全局"
        key = (k, ctx_label, new_eff)
        is_applied = prev.get(key, applied)
        recs.append({
          "key": k,
          "category": cat,
          "context": ctx_label,
          "ctx_key": ctx,
          "old_value": old_eff,
          "new_value": new_eff,
          "reason": reason,
          "applied": bool(is_applied),
        })
    try:
      with open(path, "w") as f:
        json.dump(recs, f, ensure_ascii=False, indent=2)
    except Exception:
      pass

  # ------------------------------------------------------------- 主循环步
  # ------------------------------------------------------------- 选择性应用(UI 勾选后落地)
  def _mark_applied(self, sel):
    """把 UI 勾选应用的建议在 recommendations.json 里标记为 applied=True(仅更新匹配项)。"""
    try:
      path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "carrot_learn_recommendations.json")
      if not os.path.exists(path):
        return
      with open(path, "r") as f:
        arr = json.load(f)
      for it in sel:
        k = it.get("key"); ck = it.get("ctx_key", ""); nv = int(it.get("new_value", 0))
        for rec in arr:
          if rec.get("key") == k and rec.get("ctx_key", "") == ck and int(rec.get("new_value", 0)) == nv:
            rec["applied"] = True
      with open(path, "w") as f:
        json.dump(arr, f, ensure_ascii=False, indent=2)
    except Exception as e:
      cloudlog.warning(f"carrot_learner: _mark_applied failed: {e}")

  def _apply_selected(self, sel):
    """UI 在[自动调参记录]里勾选的(待应用)建议, 由本函数真正写入 Params 并标记 applied。
    sel: list of {key, ctx_key, new_value}。已生效参数在对应驾驶情境下立即/届时生效。"""
    try:
      if not isinstance(sel, list) or not sel:
        return
      applied_any = False
      for it in sel:
        k = it.get("key")
        ctx_key = it.get("ctx_key", "")
        new_value = int(it.get("new_value", 0))
        if k not in KNOB_SPECS:
          continue
        spec = KNOB_SPECS[k]
        base = self.baseline.get(k, self._get_knob(k))
        off = int(_clip(new_value - base, spec["safe_lo"] - base, spec["safe_hi"] - base))
        self.offset[k][ctx_key] = off
        applied_any = True
        cloudlog.info(f"carrot_learner: UI applied {k}[{ctx_key}] -> {new_value}")
      if not applied_any:
        return
      self._project_feasible()
      self._apply(self.last_ctx)
      for it in sel:
        k = it.get("key")
        if k not in KNOB_SPECS:
          continue
        ctx_key = it.get("ctx_key", "")
        new_value = int(it.get("new_value", 0))
        spec = KNOB_SPECS[k]
        base = self.baseline.get(k, self._get_knob(k))
        old_eff = int(_clip(base, spec["safe_lo"], spec["safe_hi"]))
        self._log_change(k, old_eff, new_value, "UI应用", ctx=ctx_key)
      self._mark_applied(sel)
      self._save_state()
      self.params.put("CarrotLearningStatus", "已按你在[自动调参记录]的选择应用")
    except Exception as e:
      cloudlog.warning(f"carrot_learner: _apply_selected failed: {e}")

  def _revert_selected(self, sel):
    """UI 取消勾选的学习记录 -> 把对应参数回退到记录里的旧值, 并清除该参数所有情境偏移/EMA。
    sel: list of {param, ctx, old, new}。按 seq 从旧到新处理, 同参数以最新记录为准。"""
    if not sel:
      return
    # 按 seq 排序(如果存在), 保证从旧到新处理; 缺省时保持原顺序
    sel_sorted = sorted(sel, key=lambda x: int(x.get("seq", 0)))
    for it in sel_sorted:
      key = it.get("param", "")
      ctx = it.get("ctx", "")
      old_value = int(it.get("old", 0))
      if not key or key not in KNOB_SPECS:
        continue
      spec = KNOB_SPECS[key]
      # 直接写回旧值, 并以旧值为新基线, 清空该参数所有学习状态
      old_value = int(_clip(old_value, spec["safe_lo"], spec["safe_hi"]))
      cur = self._get_knob(key)
      if cur != old_value:
        self.params.put_int(key, old_value)
      self.baseline[key] = old_value
      self.offset[key] = {}
      self.ema[key] = {}
      self.last_written[key] = old_value
      self._log_change(key, cur, old_value, "UI撤销", ctx=ctx)
      cloudlog.info(f"carrot_learner: UI reverted {key}[{ctx}] -> {old_value}")
    self._save_state()

  def _delete_selected(self, sel):
    """UI 勾选的学习记录 -> 仅从 changes.json 移除该记录条目, 不回退/修改任何已学到的参数(保留参数效果)。"""
    if not sel:
      return
    del_seqs = set()
    for it in sel:
      try:
        del_seqs.add(int(it.get("seq", -1)))
      except Exception:
        pass
    if not del_seqs:
      return
    # 仅移除记录条目, 不动参数(保留已学习到的参数效果)
    before = len(self.change_log)
    self.change_log = [c for c in self.change_log if int(c.get("seq", -1)) not in del_seqs]
    removed = before - len(self.change_log)
    if removed > 0:
      self._save_changes()
      self.params.put("CarrotLearningStatus", "已删除 %d 条学习记录 (参数保持不变)" % removed)
      cloudlog.info("carrot_learner: deleted %d change records seq=%s (params untouched)" % (removed, sorted(del_seqs)))

  def update(self, sm):
    # 读取开关 / 重置
    try:
      self.enabled = self.params.get_bool("CarrotLearningEnabled")
    except Exception:
      self.enabled = False
    try:
      reset_flag = self.params.get_int("CarrotLearningReset")
    except Exception:
      reset_flag = 0

    # 兜底: 审查模式开关若从未写入过(读不到), 初始化为 0 (默认自动应用), 避免读到 None 引发异常
    # 注意: 仅当 key 完全不存在时才写, 绝不覆盖用户在 UI 上的设定
    try:
      if self.params.get("CarrotLearningReview") is None:
        self.params.put_int("CarrotLearningReview", 0)
    except Exception:
      pass

    # UI 选择性应用: 检测 C++ 端写入的勾选列表, 收到即应用并清空(防重复)
    try:
      apply_raw = self.params.get("CarrotLearningApplyList")
      if apply_raw:
        try:
          self.params.delete("CarrotLearningApplyList")
        except Exception:
          self.params.put("CarrotLearningApplyList", "")
        if isinstance(apply_raw, bytes):
          apply_raw = apply_raw.decode("utf-8")
        sel = json.loads(apply_raw)
        self._apply_selected(sel)
    except Exception as e:
      cloudlog.warning(f"carrot_learner: apply list poll failed: {e}")

    # UI 选择性撤销: 检测 C++ 端写入的取消勾选列表, 收到即回退旧值并清空(防重复)
    try:
      revert_raw = self.params.get("CarrotLearningRevertList")
      if revert_raw:
        try:
          self.params.delete("CarrotLearningRevertList")
        except Exception:
          self.params.put("CarrotLearningRevertList", "")
        if isinstance(revert_raw, bytes):
          revert_raw = revert_raw.decode("utf-8")
        rev = json.loads(revert_raw)
        self._revert_selected(rev)
    except Exception as e:
      cloudlog.warning(f"carrot_learner: revert list poll failed: {e}")

    # UI 选择性删除: 检测 C++ 端写入的删除列表, 收到即回退参数并移除对应记录(防重复)
    try:
      del_raw = self.params.get("CarrotLearningDeleteList")
      if del_raw:
        try:
          self.params.delete("CarrotLearningDeleteList")
        except Exception:
          self.params.put("CarrotLearningDeleteList", "")
        if isinstance(del_raw, bytes):
          del_raw = del_raw.decode("utf-8")
        dels = json.loads(del_raw)
        self._delete_selected(dels)
    except Exception as e:
      cloudlog.warning(f"carrot_learner: delete list poll failed: {e}")

    try:
      self.curve_enabled = self.params.get_bool("CarrotLearningCurve")
    except Exception:
      self.curve_enabled = True
    try:
      self.steer_enabled = self.params.get_bool("CarrotLearningSteer")
    except Exception:
      self.steer_enabled = True

    if reset_flag == 1:
      self._do_reset()

    # 开启学习的瞬间: 若没有基线则记录基线
    if self.enabled and not self.prev_enabled_param:
      if not self.baseline:
        self._capture_baseline()
      self.params.put("CarrotLearningStatus", "学习已开启, 等待行驶数据…")
      # 立即按当前情境(全空桶=基线)应用一次, 确保参数处于基线中心
      self._apply(self.last_ctx)
    if not self.enabled and self.prev_enabled_param:
      self.params.put("CarrotLearningStatus", "学习已暂停")
    self.prev_enabled_param = self.enabled

    if not self.enabled:
      self.prev_engaged = False
      return

    if not sm.updated['carState']:
      return

    # 激活状态
    try:
      self.engaged = bool(sm['selfdriveState'].enabled)
    except Exception:
      self.engaged = False

    # 脱离边沿
    if self.prev_engaged and not self.engaged:
      self._on_disengage(sm)

    # holdoff 计时
    self.harsh_decel_holdoff = max(0.0, self.harsh_decel_holdoff - DT_MDL)
    self.harsh_accel_holdoff = max(0.0, self.harsh_accel_holdoff - DT_MDL)
    self.osc_holdoff = max(0.0, self.osc_holdoff - DT_MDL)
    self.steer_help_holdoff = max(0.0, self.steer_help_holdoff - DT_MDL)

    if self.engaged:
      self._observe(sm)
      self.engaged_time += DT_MDL
      self.session_engaged_time += DT_MDL
      self.time_since_adapt += DT_MDL
      if self.time_since_adapt >= ADAPT_INTERVAL_S:
        self.time_since_adapt = 0.0
        self._adapt()

    self.prev_engaged = self.engaged


def main():
  cloudlog.info("carrot_learner: starting")
  learner = CarrotLearner()
  sm = messaging.SubMaster(['carState', 'selfdriveState', 'radarState'])
  rk = Ratekeeper(20, print_delay_threshold=None)  # 20Hz, 与 modelV2 同频足矣

  while True:
    sm.update(0)
    try:
      learner.update(sm)
    except Exception as e:
      cloudlog.exception(f"carrot_learner: update error: {e}")
    rk.keep_time()


if __name__ == "__main__":
  main()
