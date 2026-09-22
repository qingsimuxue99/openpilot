#!/usr/bin/env python3
"""
静止障碍横向避让 v2（独立模块，不修改原横向控制代码）

与 v1 的根本区别
----------------
v1 是「车道内让位 + 擦着过」，被真实行车数据证明是错的（96.7% 场景让位量差 5 倍，
且坐标系反号会让车朝障碍偏）。v2 按以下三条重做：

1. **无视车道线**：允许越过本车道线，最多越**一条**车道线（借入邻车道）；
   让位后的车身外沿必须留在「邻车道靠外那条线 / 道路边缘」之内并留出余量。
2. **安全间距是硬门槛**：让位后车对车横向间距必须 ≥ `AvoidSafeGap`；
   做不到 ⇒ **横向一点不让**（不再有"尽力而为"的半吊子让位）。
3. **让不开交纵向**：写 `AvoidNarrowCorridor` 让纵向减速（纵向侧接口已就绪）。

坐标系（★ 用真实行车数据锁定，见 `avoid_deploy/REPLAY_RESULT.md`）
-----------------------------------------------------------------
  正方向:  modelV2 的 y（position / laneLines / roadEdges）→ **向右为正**
           radarState.leadOne.yRel                        → **向左为正**
  ⇒ 障碍在本车 path 坐标系里的横向偏移： yRel_R = **-leadOne.yRel**（必须取负！）
     视觉源 modelV2.leadsV3[i].y 已经是右为正，直接用。

  laneLines 索引（`lane_planner_2.parse_model` 实测）:
     [1] = 本车道左线   [2] = 本车道右线
     [0] = 左邻车道**外侧**线   [3] = 右邻车道**外侧**线   ← 借道边界就取这两条
  roadEdges: [0] 左路沿(负) / [1] 右路沿(正) —— 物理兜底，只用于收紧，不用于放宽。

为什么单帧内不会跨帧累积（v1 曾因此加了个把功能卡死的 d_prob 闸门）
----------------------------------------------------------------
`lateral_planner.update()` 每帧先用 `modelV2.position` **整体重建** `path_xyz`（L112），
再调 `LP.get_d_path()`；`get_d_path` 里的 `path_xyz[:,1] = d_prob*lane + (1-d_prob)*path_xyz[:,1]`
混合的是**当帧新鲜路径**，不是上一帧的输出 ⇒ **没有反馈放大**，
d_prob 无需设闸门（v1 要求 d_prob≥0.95，实测常常不满足，等于把功能永久关掉）。

为什么用「整条路径等量平移」而不是"障碍前平移 + 障碍后渐变"
----------------------------------------------------------
等量平移不改变 dy/dx ⇒ 航向角与曲率**完全不变**，因此：
  * 不必重算 yaw，也就不必去动 `lateral_planner` 里 `if self.lanelines_active:` 那段；
  * 不会出现"路径偏了但航向没跟着偏"的不一致（v1 的 taper 会引入 ~9° 的航向偏差）。
横向几何约束在**障碍所在距离**处校验——那正是决定会不会撞的点。

三条硬要求（设计约束）
----------------------
1) **不能出现故障**：`update()` 整体 try/except；异常 ⇒ 复位 + 标志清零 + 返回原对象。
   输出被夹在 [y_min, y_max] 内；让位量有硬上限与变化率上限；侧向选择有锁定，防抖。
2) **开关独立**：唯一总开关 `AvoidObstacle`（默认 0）；与横向其他模块（弯道居中 /
   入弯减速 / 红绿灯）零引用；与纵向只通过两个既有标志单向通信。
3) **关闭即零影响**：`AvoidObstacle != 1` ⇒ 立即 `return path_xyz`（原对象，一个字节不改），
   并把两个运行期标志清零。

参数（键已编译进 params_pyx.so，不改键名，只重定义单位/语义到正确量级）
----------------------------------------------------------------------
  AvoidObstacle      0/1   总开关（默认 0=关）
  AvoidTriggerDist   m     触发距离上限（默认 35），比这更远不理会
  AvoidOffsetLimit   cm    横向让位量硬上限（默认 250 = 2.5m；实测达标需 2.26~2.40m）
  AvoidSafeGap       cm    **车对车安全间距硬门槛**（默认 60）
  AvoidEdgeMargin    cm    让位后车身外沿距边界（邻车道外线/路沿）的余量（默认 30）
  AvoidMinPassWidth  cm    允许借道所需的邻车道最小宽度（默认 200）
  AvoidActive        0/1   运行期标志（本模块写/纵向读）：正在横向让位
  AvoidNarrowCorridor 0/1  运行期标志（本模块写/纵向读）：无法安全通过 ⇒ 请纵向减速

与纵向的分工（纵向侧代码已存在，本模块只喂标志）
  AvoidActive=1         ⇒ v_cruise *= 0.85
  AvoidNarrowCorridor=1 ⇒ v_cruise = min(v_cruise, 5km/h)
     ⚠️ 纵向那行**故意不锁 0**（注释原文：「不强行锁 0 (避免锁死 ACC)」），
        是否真正停车由 carrot 的 xState 状态机决定。本模块不越权改纵向文件。
"""

import time

import numpy as np

from openpilot.common.params import Params

OFF = 0
ON = 1

# ---------------- 内部安全常量（不暴露给用户；改动等于改安全边界） ----------------
CAR_HALF_W = 0.90            # m，本车半宽
OBS_HALF_W = 1.05            # m，障碍半宽（按 2.1m 宽乘用车/SUV 保守取；卡车更宽靠安全间距兜）
MIN_OBS_DREL = 4.0           # m，比这更近不再作"新触发"（已来不及，且近场不可靠）
MAX_ABS_YREL = 6.0           # m，障碍横向位置超出此范围视为数据不可信
# ★ 本车道横向半宽上限：障碍 yRel 必须落在此范围内，才认作"需避让的本车道障碍"。
#   旁车道车（无论动静、堵不堵）yRel 通常 >2m，会被此闸门排除——它们不在你车上，
#   不应触发本车道横向避让（即修复"旁车道有车就左偏"的误触发）。旁车道信息仍被
#   借道绕障逻辑（_bound_at 靠车道线放宽走廊）使用，本闸门不影响它，故不是删功能。
LANE_HALF_W = 1.8           # m，≈标准车道半宽；本车道内偏置的静止障碍（yRel<1.8）仍正常触发避让
# ★ 队列判据（区分"孤立停靠的静止车"与"车流里排队的一辆"）：
#   前方若还有第二辆近距离 lead，说明这是车流/排队 ⇒ 不应绕行，交纵向跟停。
QUEUE_DIST = 40.0           # m，第二辆 lead 在此距离内即视为"前方有车流"
QUEUE_MIN_GAP = 5.0         # m ★ 判定的车必须比障碍**更远**至少这么多，才算"障碍前方还有车"
#   ★ 为什么必须加这条：本车 radarState.leadTwo 经常就是 leadOne 的同一台车（同值），
#     若只按"4~40m 内有第二辆"判定，孤立停靠的静止车会被自己误判成"车流" ⇒ 永远不绕。
#     正确语义 = "障碍的前方还有别的车"（排队），所以必须 d2 > d_obs + QUEUE_MIN_GAP。
QUEUE_BAND = 2.00           # m，队列判据只对横向偏移小于此值的障碍生效（更远处的本就不该绕）
VLEAD_STATIC = 1.5           # m/s，目标速度低于此视为"静止/极慢"（≈5.4km/h）
MIN_SPEED_KMH = 10.0         # km/h，**起手**速度门槛（见 _gates_ok 注释：只在"新发起"时检查）
#   ★ 实测（晚高峰 69055 帧真实数据）：威胁帧车速 p25=2.2 / p50=12.0 / p75=20.8 km/h，
#     门槛设 15 会砍掉一半场景。且门槛过高会造成"限速振荡"：narrow ⇒ 车减速到 5km/h
#     ⇒ 掉到门槛下 ⇒ 闸门失效 ⇒ narrow 撤销 ⇒ 车又加速。故速度只作起手条件，不做维持条件。
#   ⚠️ 已删除 v1 的 |steeringAngleDeg|>30° 闸门：实测威胁帧 |steer| p50=79.7°、p90=188°，
#     那是方向盘零位偏置/轮转角量纲问题，不是"正在转弯"的判据（真判据是 steeringPressed）。
CONFIRM_N = 6                # 连续命中帧数才介入（20Hz ⇒ 300ms）
MAX_SHIFT_RATE = 1.5         # m/s，让位量变化率上限。
#   ★ 小角度下 横向加速度 ≈ 横向速度 ⇒ 1.5 m/s 的让位速率 = 1.5 m/s²(0.15g)，
#     比人类变道（峰值 1.5~2 m/s²）更温和或相当；关键是能在障碍到达前把「安全间距」
#     滑够 —— 实测 60km/h 下 35m 触发只剩 ~2.1s，1.0 m/s 会差 0.45m 滑不到 ⇒ 通过瞬间间距被压到 15cm。
#     提到 1.5 后 2.1s 能滑 3.15m，覆盖 2.75m 的 need 还有余量。
RELEASE_FAST = 2.0           # 人接管/闸门失效时的释放倍率（尽快把参考位置还回去）
STEP_MARGIN = 1.15           # 时间可行性余量
MIN_T_AVAIL = 0.6            # s，少于这么多可用时间就不新起一次让位（太近，横向来不及）
POST_PASS_LEN = 12.0         # m，目标消失后继续保持让位量的里程（卡车很长，不能一丢就回中）
MAX_HOLD_TIME = 10.0         # s，保持超时（杜绝任何情况下的"永久保持"）
LANE_W_MIN = 2.2             # m，本车道宽合理下界
LANE_W_MAX = 6.0             # m，本车道宽合理上界
NEIGH_W_MAX = 6.5            # m，邻车道宽合理上界
MIN_ACTIVE_SHIFT = 0.01      # m，小于此让位量视为"尚未真正介入"
#   ★ 必须 < MAX_SHIFT_RATE*DT_FALLBACK(=0.05)，否则介入首帧会被挡掉、次帧补两步 ⇒ 台阶
DT_FALLBACK = 0.05           # s，时钟异常时的兜底步长（20Hz）
Y_DEDUP = 0.2                # m，两条车道线的 y 相差小于此视为同一条（去重）
BOUND_SAMPLES = (0.40, 0.70, 1.00)   # 让位区间的采样比例（× d_obs）
BOUND_MIN_X = 1.0            # m，最小采样距离
# ★ 护栏/路沿（roadEdges，硬边界）比车道线（可借道）多留的余量：
#   用户实测反馈"左边有护栏时车贴得很近、怕蹭"，故道路边缘侧额外留 30cm。
EDGE_EXTRA = 0.30            # m


class Avoidance:
  """静止障碍横向避让 v2。实例在 LateralPlanner.__init__ 里创建一次。"""

  def __init__(self):
    self.params = Params()
    self._pc = 9                 # 初值 9 ⇒ 首次调用即读参数，不留启动盲区
    self._mode = OFF

    # 参数（带安全默认值；读不到/异常就用这些）
    self._trig = 35.0            # m
    self._off_lim = 2.50         # m
    self._safe_gap = 0.80        # m
    self._edge_margin = 0.40     # m
    self._neigh_w_min = 2.00     # m
    self._max_kmh = 50.0         # km/h，0=不限制；超过此速度不再触发避让
    self._edge_extra = 0.30      # m，路沿/护栏侧的额外安全余量（UI：AvoidEdgeExtra，单位 cm）
    self._max_rate = 1.5         # m/s，让位量变化率上限（UI：AvoidMaxShiftRate，单位 ×0.1m/s）
    self._center_band = 0.60     # m，正前方判定带宽（UI：AvoidCenterBand，单位 cm）
    self._queue_check = True     # 队列判据开关（UI：AvoidQueueCheck）

    # 时序状态
    self._hit = 0
    self._live = False           # ★ 认领态：True 时才走"速率限制 → 应用"链路（含平滑退出）
    self._shift = 0.0            # 当前已施加的让位量（带符号，+ 向右 / − 向左）
    self._want = 0.0             # 本帧目标让位量
    self._narrow = 0
    self._side = 0               # 本次相遇锁定的让位侧（+1 右 / −1 左），防抖
    self._d_obs = None           # 最近一次命中时的障碍距离
    self._q_src = ""             # 队列判据命中来源（vis / radar），仅用于回放归因
    self._post_pass_m = 0.0      # 目标丢失后已行驶里程（用于"越过障碍后再回中"）
    self._hold_t = 0.0           # 保持计时
    self._fast = False           # 本帧是否走快速释放
    self._t_prev = None
    self._fault_cnt = 0
    self._last_geo = None       # 最近一次有效几何（用于应用前夹回走廊）

    # 已写出的标志缓存（只在变化时写，避免每帧 I/O）
    self._w_active = None
    self._w_narrow = None

    # ★ 开机清残影：两个运行期标志是 PERSISTENT 的，而 plannerd 只在"车通电"时才运行；
    #   若上一次会话结束时它们被留在 1，就会跨点火周期残留 ⇒ 纵向侧一上车就被无故限速。
    #   实例化（= plannerd 启动）时先强制清零一次，保证开局干净。
    try:
      self.params.put_bool_nonblocking("AvoidActive", False)
      self.params.put_bool_nonblocking("AvoidNarrowCorridor", False)
      self._w_active = 0
      self._w_narrow = 0
    except Exception:
      pass

    # 供外部/调试判读
    self.active = False
    self.debug = ""

  # ------------------------------------------------------------------ 参数
  def _read_params(self):
    """每 10 帧读一次参数；任何异常 ⇒ 当作关闭（最安全）。"""
    self._pc += 1
    if self._pc % 10 != 0:
      return
    try:
      self._mode = ON if self.params.get_int("AvoidObstacle") == 1 else OFF

      v = self.params.get_int("AvoidTriggerDist")
      self._trig = float(min(80, max(10, v))) if v > 0 else 40.0

      v = self.params.get_int("AvoidOffsetLimit")
      self._off_lim = float(min(400, max(50, v))) * 0.01 if v > 0 else 2.50

      v = self.params.get_int("AvoidSafeGap")
      self._safe_gap = float(min(150, max(20, v))) * 0.01 if v > 0 else 0.80

      v = self.params.get_int("AvoidEdgeMargin")
      self._edge_margin = float(min(100, max(10, v))) * 0.01 if v > 0 else 0.40

      v = self.params.get_int("AvoidMinPassWidth")
      self._neigh_w_min = float(min(400, max(150, v))) * 0.01 if v > 0 else 2.00

      v = self.params.get_int("AvoidMaxSpeed")
      # 0 或读不到 ⇒ 不限制（保留"高速也避让"的旧行为，可手动关）
      self._max_kmh = float(min(60, max(0, v))) if v > 0 else 0.0

      v = self.params.get_int("AvoidEdgeExtra")   # 路沿额外余量(cm)，默认30=0.30m
      self._edge_extra = float(min(100, max(0, v))) * 0.01 if v > 0 else 0.30

      v = self.params.get_int("AvoidMaxShiftRate")  # 让位速率(×0.1m/s)，默认15=1.5m/s
      self._max_rate = float(min(30, max(5, v))) * 0.1 if v > 0 else 1.5

      # ★★ 「停车等待车辆」vs「静止车辆」的主判据（横向）：障碍中心偏离本车路径小于
      #    此带宽 ⇒ 挡在车道正中央（等红灯 / 排队 / 正前方跟车）⇒ 交纵向跟停，横向不抢；
      #    大于此值 ⇒ 偏侧停靠的静止车辆 ⇒ 才允许评估横移绕行。
      v = self.params.get_int("AvoidCenterBand")
      self._center_band = float(min(150, max(20, v))) * 0.01 if v > 0 else 0.60

      v = self.params.get_int("AvoidQueueCheck")   # 1=启用队列判据（默认1）
      self._queue_check = (v != 0)
    except Exception:
      self._mode = OFF

  def _need(self):
    """达到安全间距所需的"车中心到障碍中心"横向距离。"""
    return CAR_HALF_W + OBS_HALF_W + self._safe_gap

  # ------------------------------------------------------------------ 标志写出
  def _write_flags(self, active, narrow):
    """只在值变化时写（非阻塞）。写失败无所谓：纵向读不到 ⇒ 不减速 ⇒ 与关闭一致。"""
    a = 1 if active else 0
    n = 1 if narrow else 0
    if a == self._w_active and n == self._w_narrow:
      return
    try:
      self.params.put_bool_nonblocking("AvoidActive", bool(a))
      self.params.put_bool_nonblocking("AvoidNarrowCorridor", bool(n))
      self._w_active = a
      self._w_narrow = n
    except Exception:
      pass

  # ------------------------------------------------------------------ 状态
  def _reset_all(self):
    self._hit = 0
    self._live = False
    self._side = 0
    self._d_obs = None
    self._post_pass_m = 0.0
    self._hold_t = 0.0
    self._fast = False
    self._shift = 0.0
    self._want = 0.0
    self._narrow = 0
    self._last_geo = None
    self.active = False
    self.debug = ""

  def _dt(self):
    t = time.monotonic()
    if self._t_prev is None:
      dt = DT_FALLBACK
    else:
      dt = t - self._t_prev
      if not (0.001 < dt < 0.5):
        dt = DT_FALLBACK
    self._t_prev = t
    return dt

  # ------------------------------------------------------------------ 入口
  def update(self, carrot, sm, path_xyz, LP, v_ego, CS):
    """
    返回（可能被修改过的）path_xyz。
    关闭 / 无让位需求 / 异常 ⇒ 返回**原对象**，内容不变。
    """
    self.active = False
    try:
      return self._update_inner(carrot, sm, path_xyz, LP, v_ego, CS)
    except Exception:
      # 故障兜底：绝不影响原横向逻辑
      self._fault_cnt += 1
      self._reset_all()
      self._write_flags(False, False)
      return path_xyz

  # ------------------------------------------------------------------ 主体
  def _update_inner(self, carrot, sm, path_xyz, LP, v_ego, CS):
    self._read_params()

    # ---------- ① 关闭：零影响 ----------
    if self._mode == OFF:
      self._reset_all()
      self._write_flags(False, False)
      return path_xyz

    dt = self._dt()
    self._fast = False
    # ★ 车速闸门只在"尚未认领"时生效：认领之后即使被 narrow 压到低速也必须维持声明，
    #   否则会出现 narrow→减速→掉出闸门→撤销 narrow→加速 的极限环。
    gates = self._gates_ok(LP, v_ego, CS, engage=not self._live)
    det = self._pick_obstacle(sm) if gates else None
    geo = self._geometry(sm, det["dRel"]) if det is not None else None

    # ---------- ② 决策 ----------
    if (det is not None) and (geo is not None):
      self._hit += 1
      self._d_obs = det["dRel"]
      self._post_pass_m = 0.0
      self._hold_t = 0.0
      tgt, nar, tag = self._decide(det, geo, v_ego)
      self._want = tgt
      self._narrow = nar
      self._last_geo = geo        # 本帧走廊（应用前夹回用）
      # ★★ 走廊收紧时的紧急回收：让位量是**整条路径等量平移**，而走廊随距离/曲率变化；
      #    车越靠近障碍，近场几何可能比出发时更紧 ⇒ 当前位置可能已落到本帧允许区间之外。
      #    此时必须按紧急速率（RELEASE_FAST 倍）把自己挪回走廊内，否则会长时间贴边界外。
      #    注意：这只加快"回收"，不会让目标越界（目标合规性由 _decide 保证）。
      if (self._shift > geo["s_max"] + 1e-6) or (self._shift < geo["s_min"] - 1e-6):
        self._fast = True
      if self._hit >= CONFIRM_N:
        self._live = True
        if abs(tgt) >= MIN_ACTIVE_SHIFT:
          self._side = 1 if tgt > 0 else -1

    elif not gates:
      # 人接管 / 几何不可信 ⇒ 目标归零并**快速**释放，且要求重新确认
      self._want = 0.0
      self._narrow = 0
      self._hit = 0
      self._side = 0
      self._d_obs = None
      self._post_pass_m = 0.0
      self._fast = True
      tag = "闸门失效"

    elif self._live:
      # 目标丢失（含已越过障碍进入近场 / 雷达丢点）：保持让位量，走完 POST_PASS_LEN 再回中
      self._post_pass_m += max(float(v_ego), 0.0) * dt
      self._hold_t += dt
      if self._post_pass_m < POST_PASS_LEN and self._hold_t < MAX_HOLD_TIME:
        self._want = self._shift
        self._narrow = 0
        tag = "保持(已越过)"
      else:
        self._want = 0.0
        self._narrow = 0
        self._hit = 0
        self._side = 0
        self._d_obs = None
        tag = "超程释放"

    else:
      self._want = 0.0
      self._narrow = 0
      tag = "无目标"

    # ---------- ③ 尚未认领：不改路径、不写标志 ----------
    if not self._live:
      self.debug = "确认中 %d/%d %s" % (self._hit, CONFIRM_N, tag)
      self._write_flags(False, 0)
      return path_xyz

    # ---------- ④ 速率限制（介入与退出都平滑；释放可加速） ----------
    rate = self._max_rate * (RELEASE_FAST if (self._fast and self._want == 0.0) else 1.0)
    step = rate * dt
    delta = self._want - self._shift
    if abs(delta) <= step:
      self._shift = self._want
    else:
      self._shift += step if delta > 0 else -step

    if abs(self._shift) < MIN_ACTIVE_SHIFT:
      # ★★ **绝不能无条件把 _shift 归零**：本函数每帧只让 _shift 前进
      #    MAX_SHIFT_RATE*dt ≈ 0.05m，若在此把 <0.01 的值清零，累加值会被反复抹掉
      #    ⇒ 让位量永远停在 0（v1 真实踩到过：决策全对但车一动不动）。
      #    只有"目标本身已归零"时才做真正的收尾。
      if abs(self._want) < MIN_ACTIVE_SHIFT:
        self._shift = 0.0
        # ★★ 但**只要 narrow 声明还在，认领态就必须维持**：
        #    否则 `_live`/`_hit` 每 CONFIRM_N 帧被复位一次 ⇒ 下一帧走"尚未认领"分支
        #    写 `_write_flags(False, 0)` ⇒ narrow 变成 1/6 占空比的闪烁，
        #    纵向侧实际收不到减速指令（功能静默失效，实测踩到过）。
        if self._narrow == 0:
          self._live = False
          self._hit = 0
          self._side = 0
          self._d_obs = None
          self._post_pass_m = 0.0
          self._hold_t = 0.0
      self.active = False
      self._write_flags(False, self._narrow)
      self.debug = "未生效(%s)" % tag
      return path_xyz

    # ---------- ⑤ 应用 ----------
    # ★ 应用前把让位量夹回本帧走廊：决策那一步只保证"目标"在走廊内，但 self._shift
    #   是上一帧速率限制的结果，若本帧走廊逐帧收紧（车靠近障碍、几何变窄），它会短暂超出。
    #   不夹回就会真实越出邻车道外线/路沿（实测峰值 0.613m）。夹回是安全兜底，不影响 ramp。
    if self._last_geo is not None:
      self._shift = min(max(self._shift, self._last_geo["s_min"]), self._last_geo["s_max"])

    if not self._apply(path_xyz, self._shift):
      self.active = False
      self._write_flags(False, self._narrow)
      self.debug = "应用被拒(%s)" % tag
      return path_xyz

    self.active = True
    self._write_flags(True, self._narrow)
    self.debug = "让位%+.2fm %s%s" % (self._shift, tag, " 让不开" if self._narrow else "")
    return path_xyz

  # ------------------------------------------------------------------ 闸门
  def _gates_ok(self, LP, v_ego, CS, engage):
    """
    闸门分两类：
      · **维持类**（每帧都查，失效立即交还控制权）：人在转向 / 打灯 / 自动变道中
      · **起手类**（只在"新发起一次让位"时查）：车速 ≥ MIN_SPEED_KMH
        ★ 车速**不能**作为维持条件：narrow 会让纵向把车速压到 5km/h，
          若速度闸门持续生效就会"narrow→减速→掉出闸门→撤销 narrow→加速"的极限环。
    ⚠️ 这里**故意不含车道线 prob 闸门**：实测 laneLineProbs 普遍 0.0x~0.6，
    几何完全可用却被 prob 卡死。几何可用性改由 `_geometry()` 的合理性判据保证。
    """
    try:
      if bool(getattr(CS, "steeringPressed", False)):
        return False
      if bool(getattr(CS, "leftBlinker", False)) or bool(getattr(CS, "rightBlinker", False)):
        return False
      if float(getattr(LP, "lane_change_multiplier", 1.0)) < 0.5:
        return False
      if engage and float(v_ego) * 3.6 < MIN_SPEED_KMH:
        return False
      # ★ 速度上限闸门（用户要求：例如设 50，则 >50km/h 不再触发避让）。
      #   维持类（每帧查）：超过上限即交还控制权；若此前已因 narrow 减速到 5km/h，
      #   会重新低于上限而重新允许——但此时已很慢且正在通过，不构成"忽快忽慢"的极限环。
      #   _max_kmh==0 视为"不限制"。
      if self._max_kmh > 0 and float(v_ego) * 3.6 > self._max_kmh:
        return False
      return True
    except Exception:
      return False

  # ------------------------------------------------------------------ 感知
  def _is_queue(self, sm, d_obs):
    """
    队列判据：障碍的**前方还有别的车** ⇒ 这是"车流/排队"，障碍只是车流里的一辆，
    不是孤立停靠的静止车辆 ⇒ 不绕行（交纵向跟停）。

    ★ 判定的车必须比障碍更远：`d > d_obs + QUEUE_MIN_GAP`。
      否则 radarState.leadTwo 常与 leadOne 同值（同一台车）会把孤立静止车误判成车流。
    数据源：modelV2.leadsV3[1]（第二近视觉 lead）与 radarState.leadTwo。
    `_q_src` 记录命中来源（vis / radar），供回放归因。
    """
    self._q_src = ""
    thr = d_obs + QUEUE_MIN_GAP
    try:
      lv = sm['modelV2'].leadsV3
      if lv is not None and len(lv) >= 2 and len(lv[1].x) > 0:
        if float(lv[1].prob) >= 0.50:
          x1 = float(lv[1].x[0])
          if thr < x1 < QUEUE_DIST:
            self._q_src = "vis"
            return True
    except Exception:
      pass
    try:
      l2 = sm['radarState'].leadTwo
      if bool(getattr(l2, "status", False)):
        d2 = float(getattr(l2, "dRel", 0.0))
        if thr < d2 < QUEUE_DIST:
          self._q_src = "radar"
          return True
    except Exception:
      pass
    return False

  def _pick_obstacle(self, sm):
    """
    返回 {'yRel','dRel','vLead','conf','src'} 或 None。
    ★ `yRel` 一律已转换到 **本车 path 坐标系（右为正）**。
    """
    # ① 主源：radarState.leadOne（yRel 左为正 ⇒ 取负）
    try:
      lead = sm['radarState'].leadOne
      if bool(getattr(lead, "status", False)):
        dRel = float(getattr(lead, "dRel", 0.0))
        yRel = -float(getattr(lead, "yRel", 0.0))     # ★ 坐标系转换
        vLead = float(getattr(lead, "vLead", 0.0))
        conf = float(getattr(lead, "modelProb", 0.0))
        radar = bool(getattr(lead, "radar", False))
        if (vLead < VLEAD_STATIC
            and MIN_OBS_DREL < dRel < self._trig
            and (conf >= 0.40 or radar)
            and np.isfinite(yRel) and abs(yRel) < MAX_ABS_YREL
            and abs(yRel) < LANE_HALF_W):
          return {"yRel": yRel, "dRel": dRel, "vLead": vLead, "conf": conf, "src": "radar",
                  "queue": self._is_queue(sm, dRel)}
    except Exception:
      pass

    # ② 兜底：模型视觉 lead（modelV2 的 y 已是右为正，直接用）
    try:
      lv = sm['modelV2'].leadsV3
      if lv is not None and len(lv) > 0 and len(lv[0].x) > 0 and len(lv[0].y) > 0:
        prob = float(lv[0].prob)
        dRel = float(lv[0].x[0])
        yRel = float(lv[0].y[0])                      # ★ 无需换号
        vLead = float(lv[0].v[0]) if len(lv[0].v) > 0 else 0.0
        if (prob >= 0.55
            and vLead < VLEAD_STATIC
            and MIN_OBS_DREL < dRel < self._trig
            and np.isfinite(yRel) and abs(yRel) < MAX_ABS_YREL
            and abs(yRel) < LANE_HALF_W):
          return {"yRel": yRel, "dRel": dRel, "vLead": vLead, "conf": prob, "src": "vision",
                  "queue": self._is_queue(sm, dRel)}
    except Exception:
      pass

    return None

  # ------------------------------------------------------------------ 几何
  def _bound_at(self, md, x, y_ref):
    """
    在纵向距离 x 处解出横向可行区间 (y_min, y_max, l1, r1, borrow_l, borrow_r)；None ⇒ 不可信。
    y_min 越负 = 越允许向左；借道 = 用邻车道外线把区间**放宽**；路沿只用于**收紧**。
    `y_ref` = 本车在该距离的路径 y，用它界定"本车道是哪两条线之间"（弯道上必须按距离取，
    不能用本车当前纵轴 0 一刀切）。
    """
    # 所有车道线在 x 处的 y（去重 + 合理区间过滤）
    vals = []
    n_lines = min(4, len(md.laneLines))
    for i in range(n_lines):
      try:
        lx = np.asarray(md.laneLines[i].x, dtype=float)
        ly = np.asarray(md.laneLines[i].y, dtype=float)
      except Exception:
        continue
      if lx.size < 2 or ly.size != lx.size:
        continue
      if not np.all(np.isfinite(lx)) or not np.all(np.isfinite(ly)):
        continue
      # ★★ 有效范围守卫：np.interp 对超出 x 范围的点会**静默钳位成端点值**，
      #    于是"40m 处的车道线"实际是"20m 处的值"，与同距离的路径 y 完全对不上，
      #    会造出偏离数米的假边界（实测踩到：越界幅度 4~5m）。
      #    取不到就是取不到，宁可判不可信也不能编。
      if not (float(lx[0]) - 1e-6 <= x <= float(lx[-1]) + 1e-6):
        continue
      v = float(np.interp(x, lx, ly))
      if (not np.isfinite(v)) or abs(v) > 12.0:
        continue
      if any(abs(v - u) < Y_DEDUP for u in vals):
        continue
      vals.append(v)

    y_min = -np.inf
    y_max = np.inf
    l1 = r1 = None
    borrow_l = borrow_r = False
    if len(vals) >= 2:
      vals.sort()
      below = [v for v in vals if v <= y_ref]
      above = [v for v in vals if v > y_ref]
      if below and above:
        l1 = max(below)          # 本车道左线（小 y）
        r1 = min(above)          # 本车道右线（大 y）
        lane_w = r1 - l1
        if (LANE_W_MIN <= lane_w <= LANE_W_MAX) and np.isfinite(lane_w):
          m = self._edge_margin
          y_min = l1 + CAR_HALF_W + m        # 不借道时的左界
          y_max = r1 - CAR_HALF_W - m        # 不借道时的右界
          outer_l = max([v for v in vals if v < l1 - Y_DEDUP], default=None)
          outer_r = min([v for v in vals if v > r1 + Y_DEDUP], default=None)
          if outer_l is not None and self._neigh_w_min <= (l1 - outer_l) <= NEIGH_W_MAX:
            y_min = min(y_min, outer_l + CAR_HALF_W + m)   # 放宽（越一条线）
            borrow_l = True
          if outer_r is not None and self._neigh_w_min <= (outer_r - r1) <= NEIGH_W_MAX:
            y_max = max(y_max, outer_r - CAR_HALF_W - m)   # 放宽（越一条线）
            borrow_r = True

    # 路沿：物理兜底，只收紧（即使车道线不全也生效）
    try:
      if len(md.roadEdges) >= 2:
        e0x = np.asarray(md.roadEdges[0].x, dtype=float)
        e0y = np.asarray(md.roadEdges[0].y, dtype=float)
        e1x = np.asarray(md.roadEdges[1].x, dtype=float)
        e1y = np.asarray(md.roadEdges[1].y, dtype=float)
        if e0x.size >= 2 and e1x.size >= 2:
          # ★ 同样必须守 x 有效范围：两条路沿都覆盖 x 才可用（否则钳位成假值）
          if not (float(e0x[0]) <= x <= float(e0x[-1])):
            raise ValueError("edge0 out of range")
          if not (float(e1x[0]) <= x <= float(e1x[-1])):
            raise ValueError("edge1 out of range")
          a = float(np.interp(x, e0x, e0y))
          b = float(np.interp(x, e1x, e1y))
          if np.isfinite(a) and np.isfinite(b):
            # ★ 路沿是硬边界（护栏/隔离带/路肩），比车道线（可借道）多留 EDGE_EXTRA 余量，
            #   直接回应"左边有护栏时车贴得很近、怕蹭"的实测反馈。
            m = self._edge_margin + self._edge_extra
            y_min = max(y_min, min(a, b) + CAR_HALF_W + m)
            y_max = min(y_max, max(a, b) - CAR_HALF_W - m)
    except Exception:
      pass

    if not (y_max > y_min):
      return None
    return (float(y_min), float(y_max), l1, r1, borrow_l, borrow_r)

  def _geometry(self, sm, d_obs):
    """
    在让位**实际作用区段**上多点采样，取可行区间的交集：
      x ∈ 按比例采样到 d_obs，再加 [d_obs, d_obs+POST_PASS_LEN]（越过障碍后的保持段）
    ★ 为什么必须多点：让位是"整条路径等量平移"，弯道上"障碍那一点能过"不等于"整段都在车道内"。
      单点校验是 v1 的另一个隐患（只在 d_obs 校验）。
    返回 None ⇒ 几何不可信 ⇒ 不介入。
    """
    md = sm['modelV2']
    px = np.asarray(md.position.x, dtype=float)
    py = np.asarray(md.position.y, dtype=float)
    if px.size < 2 or py.size != px.size:
      return None
    if not np.all(np.isfinite(px)) or not np.all(np.isfinite(py)):
      return None
    y_path = float(np.interp(d_obs, px, py))
    if not np.isfinite(y_path):
      return None

    b_obs = self._bound_at(md, d_obs, y_path)
    if b_obs is None:
      return None

    xs = [max(BOUND_MIN_X, d_obs * f) for f in BOUND_SAMPLES]
    xs += [d_obs, d_obs + POST_PASS_LEN * 0.5, d_obs + POST_PASS_LEN]
    xs = sorted(set(round(v, 3) for v in xs if BOUND_MIN_X <= v <= 120.0))

    # ★★ 关键：让位是"整条路径等量平移 shift"，所以真正要约束的不是"某个绝对 y"，
    #    而是"允许的 shift 区间"。逐采样点把**绝对**边界换算成**相对本车路径**的约束：
    #       对每个 x：y_min(x) ≤ py(x)+shift ≤ y_max(x)
    #                ⇔ y_min(x)-py(x) ≤ shift ≤ y_max(x)-py(x)
    #       对所有 x 取交集 ⇒ shift ∈ [s_min, s_max]
    #    这同时解决了两个问题：
    #      ① 不同 x 的边界本就属于不同横截面，直接对绝对 y 求交集再拿单点 y_path 去比
    #         是**语义错位**（会把"本来就该能过"的场景判死 ⇒ 决策永远侧向无解）；
    #      ② 换算回绝对区间（y_path+s_min / y_path+s_max）后，_decide 的比较与
    #         仿真里 "y_path+shift 是否越界" 的校验完全等价，是同一个真约束。
    s_min = -np.inf
    s_max = np.inf
    for x in xs:
      y_ref = float(np.interp(x, px, py))
      if not np.isfinite(y_ref):
        return None
      b = self._bound_at(md, x, y_ref)
      if b is None:
        return None
      s_min = max(s_min, b[0] - y_ref)
      s_max = min(s_max, b[1] - y_ref)
    if (not np.isfinite(s_min)) or (not np.isfinite(s_max)) or not (s_max > s_min):
      return None

    # ★★★ 自洽性闸门（几何可信度的唯一硬判据）：
    #   本车正在行驶的这条路径，**必须落在自己算出来的走廊内**，即 0 ∈ [s_min, s_max]。
    #   若 s_min > 0 或 s_max < 0，说明"路径"与"车道线/路沿"互相矛盾
    #   （实测典型：y_path=-6.42 而路沿给出的走廊是 [-2.39,-2.13]，路径在走廊外 4m）。
    #   这种情况下任何让位决策都是建立在垃圾几何上的 ⇒ 一律判不可信、不介入。
    if s_min > 1e-6 or s_max < -1e-6:
      return None

    lane_w = (float(b_obs[3]) - float(b_obs[2])) if (b_obs[2] is not None) else float('nan')
    return {"y_path": y_path, "y_min": y_path + float(s_min), "y_max": y_path + float(s_max),
            "s_min": float(s_min), "s_max": float(s_max),
            "lane_w": lane_w, "n_samples": len(xs),
            "l1": b_obs[2], "r1": b_obs[3],
            "borrow_l": bool(b_obs[4]), "borrow_r": bool(b_obs[5])}

  # ------------------------------------------------------------------ 决策
  def _decide(self, det, geo, v_ego):
    """
    返回 (目标让位量 m, narrow 0/1, 标签)。
    规则（按优先级）：
      间距足够        |d| ≥ need                  ⇒ 不介入
      正前方目标      |d| < CENTER_BAND           ⇒ 不介入（等红灯/排队/正前方跟车，交纵向）
      前方车流        队列判据命中且 |d| < QUEUE_BAND ⇒ 不介入（车流里的一辆，交纵向）
      侧方侵入且可让  要求位置落在 [y_min,y_max] 且让位量 ≤ 上限 ⇒ 让位（安全间距保证达成）
      侧方侵入不可让  上面做不到              ⇒ 不让位 + narrow=1（交纵向减速）

    ★★ 为什么把"正前方"判据从 OBS_HALF_W(1.05m) 收窄为 CENTER_BAND(0.60m)：
      实测一次 48 分钟真实行程中，`_pick_obstacle` 命中的 5396 帧里有 **99.7% 的 |d| < 1.05**
      （障碍几乎压在本车路径正中心）⇒ 全部被判"正前方目标"、直接交纵向 ⇒ **避让一次都没动作**
      （回放 28.5 万帧 `act=0`）。根因：1.05m 是"障碍半宽"，拿它当"正前方"判据等于把所有
      障碍都吃掉。改成 CENTER_BAND 后语义才正确：**车道正中央**的静止车交纵向（跟停 / 等灯 /
      排队），**偏侧停靠**的静止车才评估横移绕行 —— 这就是"正确识别静止车辆 vs 停车等待车辆"。
    """
    need = self._need()
    y_path = geo["y_path"]
    y_obs = det["yRel"]                      # 障碍绝对横向位置（path 坐标系）
    d = y_obs - y_path                       # 障碍相对本车路径的偏移（右为正）
    if not np.isfinite(d):
      return (0.0, 0, "数据异常")

    if abs(d) >= need:
      return (0.0, 0, "间距足够")

    # ★★ 主判据：「停车等待车辆」vs「静止车辆」——先看横向偏置程度。
    #   障碍中心偏离本车路径 < CENTER_BAND ⇒ 它挡在**车道正中央**（等红灯 / 排队 / 正前方
    #   跟车），横向让开它必须跨整条车道、危险且反直觉 ⇒ 交纵向跟停，横向不抢。
    if abs(d) < self._center_band:
      return (0.0, 0, "正前方目标")

    # ★ 队列判据：障碍虽偏侧，但它前方还有别的车（车流 / 排队缓行）⇒ 它是"车流里的一辆"，
    #   不是"孤立停靠的静止车辆" ⇒ 同样交纵向跟停，不做横移绕行。
    #   只有"偏侧 + 前方无队列"才认定为可绕的停靠静止车辆。
    if self._queue_check and bool(det.get("queue", False)) and abs(d) < QUEUE_BAND:
      return (0.0, 0, "前方车流")

    # 从左右两侧通过所需的**让位量**（相对本车路径，右为正）
    #   s_l<0：向左让到"障碍落在我们右侧 need 处"；s_r>0：向右让到"障碍落在我们左侧 need 处"
    # ★ 必须与 geo 的 s_min/s_max 同一语义（都是"允许的 shift 区间"），
    #   绝不能拿绝对 y 去比绝对边界——那是 v2 首轮真实踩到的错位 bug。
    s_l = (y_obs - need) - y_path
    s_r = (y_obs + need) - y_path
    ok_l = (s_l >= geo["s_min"]) and ((-s_l) <= self._off_lim)
    ok_r = (s_r <= geo["s_max"]) and (s_r <= self._off_lim)

    cands = []
    if ok_l:
      cands.append((abs(s_l), s_l, -1))
    if ok_r:
      cands.append((abs(s_r), s_r, +1))
    if not cands:
      return (0.0, 1, "侧向无解")

    # 侧向锁定（本次相遇内不来回换边，防抖）
    if self._side != 0:
      same = [c for c in cands if c[2] == self._side]
      if same:
        cands = same
    cands.sort(key=lambda c: c[0])
    mag, shift, side = cands[0]

    # 时间可行性：还需要走的让位量 vs 到达障碍还剩多少时间
    t_avail = det["dRel"] / max(float(v_ego), 0.5)
    if (not self._live) and t_avail < MIN_T_AVAIL:
      return (0.0, 1, "太近")
    remain = max(0.0, mag - abs(self._shift))
    narrow = 0 if (t_avail * self._max_rate >= remain * STEP_MARGIN) else 1
    borrowed = bool(geo["borrow_l"] if side < 0 else geo["borrow_r"])
    return (shift, narrow, "让位" + ("(借道)" if borrowed else ""))

  # ------------------------------------------------------------------ 应用
  def _apply(self, path_xyz, shift):
    """
    整条路径等量平移。★ 等量平移不改变 dy/dx ⇒ 航向/曲率不变，无需重算 yaw。
    只做完整性自检，几何约束已在 `_decide()` 按障碍所在距离校验过。
    """
    if path_xyz.shape[0] < 2 or path_xyz.shape[1] < 2:
      return False
    y = np.asarray(path_xyz[:, 1], dtype=float)
    if not np.all(np.isfinite(y)):
      return False
    if abs(shift) > self._off_lim + 1e-3:
      return False                      # 理论上不可能（_decide 已限幅）；出现即视为 bug
    path_xyz[:, 1] = y + float(shift)
    return True
