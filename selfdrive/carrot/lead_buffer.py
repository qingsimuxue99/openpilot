#!/usr/bin/env python3
"""
丢目标缓冲（独立模块，默认关＝零影响）
====================================

问题（实测数据）
----------------
`longitudinal_planner.py:313` 只把 `radarState` 喂给 MPC。radar 对小目标
（电动车/摩托，RCS 小）会间歇性丢点。一次 48 分钟真实行程（28.5 万帧）实测：

  · lead 丢失 623 次，其中**持续 ≥1.5s 的真丢失 89 次**，平均持续 **12.9s**
    （p90 37.8s / 最长 71.3s）——不是"抖一下就回来"，是真的长时间看不到车
  · 丢失前本车 21.6km/h 正跟着一台 13.2km/h 的车
  · 丢失期间最高车速平均 **+8.2km/h**，p90 +25.2，最大 **+48**
  · **36% 的事件冲超 8km/h**；只有 11.2% 期间在变道 ⇒ 不是变道造成的

根因：lead 一丢，MPC 失去跟车约束 ⇒ 目标速度回到巡航设定 ⇒ 全速冲出去。
上游自己也承认这个洞（`longitudinal_planner.py:320` "radar is glitchy"、
`:323` "Does NOT help if leadOne is fully missing"），而视觉 `leadsV3` 并未并入 MPC。

对策
----
在送 MPC 之前**包裹** radarState：lead 刚丢失时，用最后一次有效观测
（dRel / vRel / vLead / yRel）做运动学外推，构造一个"虚拟 lead"继续约束 MPC，
直到真实 lead 回来或缓冲超时（默认 2.5s，可调）。

安全约束（缺一不可）
--------------------
1. **只在"近距离跟车时丢失"才缓冲**：丢失前 lead 必须在 `LeadBufferMaxDist`
   以内，且本车在动（>1m/s）。高速巡航时丢目标（前车正常变道走了）不缓冲。
2. **虚拟 lead 只会更近不会更远**：`dRel += vRel*dt`，vRel/vLead 一律取丢失前
   的观测值（**不假设前车加速**）⇒ 外推永远比真实更保守。
   外推距离一旦超过 `LeadBufferMaxDist`（前车正在远离）立即释放。
3. **贴脸即放弃**：外推 dRel < `MIN_VIRTUAL_DREL`(5m) 立即释放 —— 这种情况本车
   确实该减速，交回纵向正常逻辑更安全。
4. **硬超时**：超过 `LeadBufferTime` 立刻释放，雷达状态直接切回真实对象
   （MPC 自身有加速度/jerk 限幅，不会产生阶跃）。
5. **双层保险**：除虚拟 lead 外，缓冲期额外给出 `accel_cap`，让纵向把加速度
   上限压到 0.6m/s²，避免"虚拟 lead 被 MPC 权重削弱"时仍然冲出去。
6. **整体 try/except**：任何异常 ⇒ 返回原 radarState，绝不影响纵向
   （异常原因写入 `self.debug`，不静默）。
7. **关闭（默认）⇒ 直接返回传入对象**，零开销、零影响。
8. **一次丢失事件只缓冲一次**：超时 / 贴脸 / 超距任一释放后 `_used` 封口，不再
   重启缓冲。否则 2.5s 到点后下一帧又会重进"刚丢失"分支 ⇒ 缓冲无限延长；且重启
   会把 `_d` 从丢失前观测重新外推（等于"时间倒流"）。必须等真实 lead 重新出现
   （哪怕只有一帧）才解禁，那才算一次新的丢失事件 —— 这同时让雷达"抖回来"
   能自动延长有效覆盖时间。

实现坑（设备实测，勿踩）
------------------------
* `log.Event.new_message()` **不带参数**时 Event 的 union 成员尚未初始化，
  此时读 `evt.radarState` 会抛
  `KjException: ... Tried to get() a union member which is not currently initialized`。
  必须先 `evt.radarState = radar_state` **赋值**（赋值本身会初始化 union），
  之后才能逐字段改写。`log.Event.new_message('radarState')` 也不行
  （参数被当成 MallocMessageBuilder 的 size ⇒ `TypeError: an integer is required`）。
* "缓冲已接入"这个内部状态**不能**复用对外可见的 `self.active`：`update()` 每帧
  开头会把 `self.active` 复位为 False，若用它当状态机会导致每帧重进"刚丢失"
  分支、`_t_buf`/`_d` 被反复重置（外推不推进、超时永不触发）。
  内部闩锁用独立的 `self._engaged`。

参数
----
  LeadBufferEnable   0/1     总开关（默认 0=关）
  LeadBufferTime     x0.1 s  缓冲时长（默认 25 = 2.5s；量程 0.5~6.0s）
  LeadBufferMaxDist  m       仅当"丢失前 lead 距离"小于此值才缓冲（默认 40）
"""

import time

from cereal import log
from openpilot.common.params import Params

OFF = 0
ON = 1

MIN_VIRTUAL_DREL = 5.0     # m，虚拟 lead 外推到这个距离就放弃（该减速了）
MIN_EGO_SPEED = 1.0        # m/s，本车低于此速度不缓冲（静止/蠕行阶段无意义）
BUFFER_ACCEL_CAP = 0.6     # m/s²，缓冲期加速度上限（双层保险）
DT_FALLBACK = 0.05         # s，时间戳异常时的兜底步长


class LeadBuffer:
  def __init__(self):
    self.params = Params()
    self.enable = OFF
    self.buf_time = 2.5
    self.max_dist = 40.0

    # 运行期状态（供调试/UI 观察）
    self.active = False         # 对外：本帧是否处于缓冲（每帧重新计算）
    self.accel_cap = None       # None = 不干预纵向加速度上限
    self.debug = ""

    self._engaged = False       # 内部闩锁：缓冲是否已接入（跨帧保持）
    self._used = False          # 本次"丢失事件"是否已用完缓冲（超时/贴脸/超距后不再重启）
    self._pc = 49               # 初值 49 ⇒ 首次调用即读参数，不留启动盲区
    self._t_prev = None
    self._t_buf = 0.0
    self._d = 0.0
    self._vrel = 0.0
    self._vlead = 0.0
    self._yrel = 0.0
    self._last = None           # 最后一次有效 lead 观测 (dRel, vRel, vLead, yRel)
    self._build_err = ""        # 虚拟 lead 构造失败原因（现场排查用）

  # ------------------------------------------------------------------ 参数
  def _read_params(self):
    # 每 50 帧（20Hz ⇒ 2.5s）读一次即可；读取失败不影响行车
    self._pc += 1
    if self._pc % 50 != 0:
      return
    try:
      self.enable = self.params.get_int("LeadBufferEnable")
      t = self.params.get_int("LeadBufferTime")
      self.buf_time = max(0.5, min(6.0, t * 0.1)) if t > 0 else 2.5
      d = self.params.get_int("LeadBufferMaxDist")
      self.max_dist = max(10.0, min(80.0, float(d))) if d > 0 else 40.0
    except Exception:
      self.enable = OFF

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

  def _release(self):
    self._engaged = False
    self.active = False
    self.accel_cap = None
    self._t_buf = 0.0

  def _reset(self):
    self._release()
    self._last = None
    self._used = False
    self._build_err = ""
    self.debug = ""

  # ------------------------------------------------------------------ 入口
  def update(self, carrot, sm, radar_state):
    """返回（可能被替换过的）radarState。关闭 / 不需缓冲 / 异常 ⇒ 返回原对象。"""
    self.active = False
    self.accel_cap = None
    try:
      return self._update_inner(sm, radar_state)
    except Exception as e:
      self._release()
      # 不静默：把原因写进 debug，便于现场定位（行车仍按原 radarState 走，安全优先）
      self.debug = "缓冲异常降级 %s: %s" % (type(e).__name__, e)
      return radar_state

  def _update_inner(self, sm, radar_state):
    self._read_params()
    if self.enable != ON:
      self._reset()
      return radar_state

    dt = self._dt()

    try:
      lead = radar_state.leadOne
      status = bool(lead.status)
    except Exception:
      return radar_state

    # ① 真实 lead 在 ⇒ 记录观测、结束缓冲
    if status:
      try:
        self._last = (float(lead.dRel), float(lead.vRel), float(lead.vLead), float(lead.yRel))
      except Exception:
        self._last = None
      self._release()
      self._used = False        # 真实观测刷新 ⇒ 下次丢失可以重新缓冲
      self.debug = "真实lead"
      return radar_state

    # ② 丢失但无历史观测 ⇒ 无从外推
    if self._last is None:
      self.debug = "无历史观测"
      return radar_state

    # ②b 本次"丢失事件"已用完缓冲（超时/贴脸/超距释放过）⇒ 不再重启，
    #     必须等真实 lead 重新出现才算新事件。否则会无限缓冲，且重启会让
    #     _d 从丢失前观测重新外推（等于"时间倒流"）。
    if self._used:
      self.debug = "本次丢失已缓冲过(等真实lead刷新)"
      return radar_state

    d0, vrel0, vlead0, yrel0 = self._last

    # ③ 刚丢失：先判断"值不值得缓冲"（只看一次，靠 _engaged 而不是 active 记状态）
    if not self._engaged:
      if d0 > self.max_dist:
        self._last = None
        self.debug = "丢失前距离过远(%.1fm)不缓冲" % d0
        return radar_state
      try:
        v_ego = float(sm['carState'].vEgo)
      except Exception:
        return radar_state
      if v_ego < MIN_EGO_SPEED:
        self._last = None
        self.debug = "本车近静止不缓冲"
        return radar_state
      self._engaged = True
      self._t_buf = 0.0
      self._d = d0
      self._vrel = vrel0
      self._vlead = vlead0
      self._yrel = yrel0

    # ④ 缓冲期：运动学外推（只按丢失前观测的相对速度推，不假设前车加速）
    self._d += self._vrel * dt
    self._t_buf += dt

    # ⚠️ 下面三个释放都用 self._used=True 封口：本帧起不再重启缓冲。
    #    注意 debug 必须在 _release() 之前格式化（_release 会把 _t_buf 清零）。
    if self._d < MIN_VIRTUAL_DREL:
      msg = "虚拟lead贴脸(%.1fm)释放" % self._d
      self._release()
      self._used = True
      self.debug = msg
      return radar_state
    if self._d > self.max_dist:
      msg = "虚拟lead超出最大距离(%.1fm)释放" % self._d
      self._release()
      self._used = True
      self.debug = msg
      return radar_state
    if self._t_buf > self.buf_time:
      msg = "缓冲超时(%.1fs)释放" % self._t_buf
      self._release()
      self._used = True
      self.debug = msg
      return radar_state

    self.active = True
    self.accel_cap = BUFFER_ACCEL_CAP

    patched = self._make_patched(radar_state)
    if patched is None:
      self.debug = "缓冲(降级:仅加速度上限) %.1fs d=%.1fm [构造失败 %s]" % (self._t_buf, self._d, self._build_err)
      return radar_state
    self.debug = "虚拟lead %.1fs d=%.1fm vLead=%.1fkm/h" % (self._t_buf, self._d, self._vlead * 3.6)
    return patched

  def _make_patched(self, radar_state):
    """构造一份 radarState 副本，把 leadOne 换成虚拟 lead（字段完整，MPC 可直读）。

    ⚠️ 必须用 `evt.radarState = radar_state` 赋值来初始化 Event 的 union 成员，
    不能先读 `evt.radarState`（未初始化会抛 KjException）。
    """
    try:
      evt = log.Event.new_message()
      evt.radarState = radar_state
      lo = evt.radarState.leadOne
      lo.status = True
      lo.dRel = float(self._d)
      lo.vRel = float(self._vrel)
      lo.vLead = float(self._vlead)
      lo.yRel = float(self._yrel)
      # 非关键字段：不同 fork 版本可能缺，逐个 try（失败也不影响虚拟 lead 生效）
      for k, v in (("radar", False), ("modelProb", 0.90),
                   ("aLead", 0.0), ("aLeadK", 0.0), ("aLeadTau", 1.5)):
        try:
          setattr(lo, k, v)
        except Exception:
          pass
      self._build_err = ""
      return evt.radarState
    except Exception as e:
      self._build_err = "%s: %s" % (type(e).__name__, e)
      return None
