#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""carrot_tuner_ai.py — 萝卜自动调参分析器 (体检 + 建议)

为什么是这个形态 (关于"c3 算力够不够 / 本地还是云端"的结论):
  * 本地跑 LLM: 不可行。SDA845 只有 4 个 CPU 核, 无可用 GPU 推理栈, AGNOS 也没有
    llama.cpp 的编译环境; 即便勉强跑起 1B 量化模型, onroad 时会直接抢占控制栈的
    实时配额 —— 这是安全红线, 不能碰。
  * 云端 LLM: 可行且廉价。本模块把学习器状态压缩成 2~5 KB 的 JSON 摘要再上传,
    不传 rlog、不传视频、不传定位, 流量与隐私成本都可以忽略。
  * 但真正的瓶颈从来不是"缺智能", 而是"缺样本"。所以本模块的主体是
    **离线本地规则诊断** —— 零依赖、零网络、毫秒级, 先把"为什么没学到"讲清楚;
    云端 AI 只是可选的锦上添花。

用法:
  python3 selfdrive/carrot/carrot_tuner_ai.py             # 本地规则分析 (默认)
  python3 selfdrive/carrot/carrot_tuner_ai.py --ai        # 追加云端 AI 深度分析
  python3 selfdrive/carrot/carrot_tuner_ai.py --summary   # 只输出摘要 JSON (调试用)

云端 API key: 车机菜单「设置云端AI密钥」写入 Param(CarrotAiCloudKey), 或放 /data/carrot_ai_key.txt (一行)。
多平台自动探测: 粘贴任意一家的 key 即可, 不用告诉程序是哪家 —— 依次试各家端点,
哪家鉴权通过就用哪家, 并把结果缓存到 /data/carrot_ai_provider.txt, 下次直连不再重试。
首推硅基流动 (cloud.siliconflow.cn): Qwen3-8B 等 9B 以下模型永久免费不限量, 手机号注册即可、无需实名。
没有 key 时 --ai 自动降级为本地分析, 不会报错。
报告同时写入 selfdrive/carrot/carrot_ai_report.txt。
"""
import os
import sys
import urllib.error
import urllib.request
import json
import re
import time
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "carrot_learn_state.json")
CHANGES_PATH = os.path.join(HERE, "carrot_learn_changes.json")
DRIVE_LOG_PATH = os.path.join(HERE, "carrot_drive_log.jsonl")
REPORT_PATH = os.path.join(HERE, "carrot_ai_report.txt")
KEY_PATH = "/data/carrot_ai_key.txt"

# AI 调参建议候选文件 (与学习器记录完全分离!)
#   * 学习器记录  -> carrot_learn_changes.json   : 学习器自己学出来的, 可自动应用
#   * AI 建议候选 -> carrot_ai_suggestions.json  : 云端 AI 提的, 只能在 UI 里人工勾选后应用
# 两者数据文件、Param 通道、UI 界面三者全部独立, 互不写入对方。
SUGGEST_PATH = os.path.join(HERE, "carrot_ai_suggestions.json")

# 参数白名单与安全区: 优先复用学习器的唯一权威定义, 避免两处漂移。
# AI 的输出一律不可信 —— 任何不在白名单、超安全区、超单次幅度上限的建议直接丢弃。
try:
  from openpilot.selfdrive.carrot.carrot_learner import KNOB_SPECS as _KNOB_SPECS, SAFE_BOUNDS as _SAFE_BOUNDS
except Exception:
  try:
    from carrot_learner import KNOB_SPECS as _KNOB_SPECS, SAFE_BOUNDS as _SAFE_BOUNDS
  except Exception:
    _KNOB_SPECS, _SAFE_BOUNDS = {}, {}

AI_MAX_STEP_RATIO = 0.15   # 单条建议相对当前值的最大变动幅度 (±15%), 防止 AI 幻觉出离谱数值
AI_MAX_ITEMS = 8           # 单次最多接受的建议条数

PROVIDER_CACHE_PATH = "/data/carrot_ai_provider.txt"   # 缓存上次鉴权成功的平台名, 避免每次重试
AI_TIMEOUT = 45          # 报告 prompt 较长, 25s 容易不够
AI_SUGGEST_TIMEOUT = 100 # 建议要输出多条结构化 JSON, 比写散文慢得多, 需单独放宽

# 多平台候选表 (全部 OpenAI 兼容, 请求体基本一致, 只差 endpoint + model + 少量平台专属参数)。
# 顺序 = 探测优先级: 永久免费且无实名门槛的排前面。
# 用户只需粘一个 key, 程序自动试出它属于哪家 —— 换平台不用改代码。
# 第 5 字段 extra = 合并进请求体的平台专属参数 (别的平台可能不认识, 故按平台隔离)。
#   Qwen3 系列默认开启 thinking 深度推理, 生成极慢直接超时 -> 必须 enable_thinking=False。
AI_PROVIDERS = [
  ("硅基流动",   "https://api.siliconflow.cn/v1/chat/completions",
                 "Qwen/Qwen3-8B",            "9B以下永久免费不限量, 手机号注册, 无需实名",
                 {"enable_thinking": False}),
  ("智谱GLM",    "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                 "glm-4-flash-250414",       "GLM-4-Flash 永久免费, 128K 上下文",
                 {}),
  ("火山方舟",   "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                 "doubao-lite-32k",          "豆包 Lite 免费额度高 (模型名需与推理接入点一致)",
                 {}),
  ("阿里百炼",   "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                 "qwen-flash",               "每模型 100 万 token/90 天, 需阿里云实名",
                 {}),
]

# 参数中文名 + 简短方向 + 完整含义说明
#   [0] 中文名   [1] 简短方向提示(值变大意味着什么)   [2] 完整说明: 是什么 + 调大/调小各自的效果
# 第 [2] 项与 UI 侧 selfdrive/ui/qt/offroad/settings.cc 的 knobExplain() 文案保持一致,
# 保证同一参数在「报告正文」「自动调参记录」「AI 调参建议」三处的解释不会各说各的。
_ACC_EXPL = ("该车速段允许的最大加速度, 值/100 = m/s^2 (如 160 = 1.6 m/s^2)。"
             "调大=起步/提速更有劲更跟脚, 但冲劲大费油; 调小=加速更平缓省油舒适, 但显肉易被别车")
_GAP_EXPL = ("跟车时距, 值/100 = 秒 (如 130 = 1.30 秒), 即与前车保持几秒车距。"
             "调大=跟车更远更从容安全, 但易被加塞; 调小=跟车更近不易被插队, 但留给刹车的余量变少")
_CURVE_EXPL = ("弯道自动降速的激进度, 即允许多大的过弯横向 G。"
               "调大=过弯少减速更快更跟脚, 但侧倾明显、湿滑路面风险大; 调小=入弯提前多减速更稳更安全, 但通过速度慢")

KNOB_INFO = {
  "TFollowGap1": ("跟车时间 GAP1 (激进档)", "大=跟得更远", _GAP_EXPL),
  "TFollowGap2": ("跟车时间 GAP2 (标准档)", "大=跟得更远", _GAP_EXPL),
  "TFollowGap3": ("跟车时间 GAP3 (宽松档)", "大=跟得更远", _GAP_EXPL),
  "TFollowGap4": ("跟车时间 GAP4 (最宽松)", "大=跟得更远", _GAP_EXPL),
  "CruiseMaxVals0": ("加速性 0-10km/h", "大=起步更冲", _ACC_EXPL),
  "CruiseMaxVals1": ("加速性 10-40km/h", "大=加速更猛", _ACC_EXPL),
  "CruiseMaxVals2": ("加速性 40-60km/h", "大=加速更猛", _ACC_EXPL),
  "CruiseMaxVals3": ("加速性 60-80km/h", "大=加速更猛", _ACC_EXPL),
  "CruiseMaxVals4": ("加速性 80-110km/h", "大=加速更猛", _ACC_EXPL),
  "CruiseMaxVals5": ("加速性 110-140km/h", "大=加速更猛", _ACC_EXPL),
  "CruiseMaxVals6": ("加速性 140+km/h", "大=加速更猛", _ACC_EXPL),
  "ComfortBrake": ("舒适制动强度", "大=刹得更硬",
                   "常规跟车减速时允许的制动强度, 值/100 = m/s^2 (如 240 = 2.4 m/s^2)。"
                   "调大=刹车更果断介入更早, 干脆留余量大; 调小=刹车更柔和线性舒适, 但可能靠得较近才减速"),
  "StopDistanceCarrot": ("停车距离", "大=停得更远",
                   "跟停后与前车保持的距离, 单位 cm (如 600 = 6.0 米)。"
                   "调大=停得更靠后更宽松安全, 但可能被加塞; 调小=停得更贴近前车队列更紧凑, 但压迫感强"),
  "AutoCurveSpeedAggressiveness": ("弯道激进度(普通路)", "大=过弯不减速", _CURVE_EXPL + " [普通路段]"),
  "AutoCurveSpeedAggressivenessH": ("弯道激进度(高速)", "大=过弯不减速", _CURVE_EXPL + " [高速路段]"),
  "LatMpcSteeringRateCost": ("转向手感", "大=更平顺/迟钝, 小=更灵敏",
                   "横向控制对「方向盘转动快慢」的代价权重, 数值越高越不愿意快打方向。"
                   "调大=转向更平顺柔和不画龙, 但切弯/避让可能迟钝; 调小=转向更积极灵敏跟手贴线准, 但方向盘动作频繁易画龙"),
}


def knob_explain(k):
  """取参数的完整含义说明 (是什么 + 调大调小各自效果)。未收录则返回空串, 不硬编造。"""
  info = KNOB_INFO.get(k)
  return info[2] if info and len(info) > 2 else ""


def knob_group(k):
  """同类参数归为一组, 返回 (组键, 组显示名)。

  报告的「参数含义速查」按组只解释一次 —— 否则 TFollowGap1~4 会把同一段话重复 4 遍,
  CruiseMaxVals0~6 重复 7 遍, 反而变成噪音。
  """
  if k.startswith("TFollowGap"):
    return ("gap", "跟车时距", _GAP_EXPL)
  if k.startswith("CruiseMaxVals"):
    return ("accel", "加速性", _ACC_EXPL)
  if k.startswith("AutoCurveSpeedAggressiveness"):
    return ("curve", "弯道激进度", _CURVE_EXPL)
  return (k, KNOB_INFO.get(k, (k,))[0], knob_explain(k))

CAT_NAME = {"follow": "跟车", "accel": "加速", "curve": "弯道", "steer": "转向", "other": "其他"}


def _load_json(path, default):
  try:
    if os.path.exists(path):
      with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
  except Exception:
    pass
  return default


def _read_learner_consts():
  """从学习器源码里取当前生效的门槛常量, 避免两处写死后不同步。"""
  out = {"MIN_SAMPLES": 2.0, "ADAPT_INTERVAL_S": 45.0, "IDLE_CYCLES_DECAY": 8,
         "ACC_CARRY": 0.8, "DEADBAND": 0.10}
  try:
    sys.path.insert(0, "/data/openpilot")
    from openpilot.selfdrive.carrot import carrot_learner as cl  # noqa
    for k in list(out.keys()):
      if hasattr(cl, k):
        out[k] = getattr(cl, k)
    out["SAFE_BOUNDS"] = dict(getattr(cl, "SAFE_BOUNDS", {}))
  except Exception:
    out["SAFE_BOUNDS"] = {}
  return out


def build_summary():
  """把学习器全部状态压缩成 2~5 KB 的结构化摘要 (这就是要发给 AI 的全部内容)。"""
  st = _load_json(STATE_PATH, {})
  ch = _load_json(CHANGES_PATH, {})
  consts = _read_learner_consts()

  # --- 行车日志聚合 ---
  n_lines, eng_max, dov_tot = 0, 0.0, {"brake": 0, "gas": 0, "curve": 0, "steer": 0}
  ctx_hist, last_recs = {}, []
  try:
    if os.path.exists(DRIVE_LOG_PATH):
      with open(DRIVE_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
          line = line.strip()
          if not line:
            continue
          try:
            r = json.loads(line)
          except Exception:
            continue
          n_lines += 1
          eng_max = max(eng_max, float(r.get("eng", 0)))
          for k, v in (r.get("dov") or {}).items():
            dov_tot[k] = dov_tot.get(k, 0) + int(v)
          for _, c in (r.get("ctx") or {}).items():
            ctx_hist[c] = ctx_hist.get(c, 0) + 1
          last_recs.append(r)
    last_recs = last_recs[-8:]
  except Exception:
    pass

  changes = (ch.get("changes") or [])[-15:]
  stats = st.get("stats") or {}
  acc = st.get("acc") or {}

  return {
    "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"),
    "学习器门槛": {k: consts.get(k) for k in
                   ("MIN_SAMPLES", "ADAPT_INTERVAL_S", "IDLE_CYCLES_DECAY", "ACC_CARRY", "DEADBAND")},
    "累计干预": {"踩刹车": st.get("brake_overrides", 0), "踩油门": st.get("gas_overrides", 0),
                 "弯道": st.get("curve_overrides", 0), "转向": st.get("steer_overrides", 0)},
    "累计调参次数": st.get("adjust_count", 0),
    "稳定度指标": st.get("instability"),
    "基线": st.get("baseline") or {},
    "已学偏移": {k: v for k, v in (st.get("offset") or {}).items() if v},
    "净奖励EMA": {k: v for k, v in (st.get("ema") or {}).items() if v},
    "样本累加器": {k: v for k, v in acc.items() if v},
    "生命周期样本": {k: v for k, v in stats.items() if v and v.get("n")},
    "行车日志": {"记录条数": n_lines, "累计激活分钟": round(eng_max, 1),
                 "日志内干预增量": dov_tot, "情境桶出现频次": ctx_hist},
    "最近调参记录": [{"参数": c.get("param"), "旧": c.get("old"), "新": c.get("new"),
                      "原因": c.get("trigger"), "情境": c.get("ctx", "")} for c in changes],
    "安全区": consts.get("SAFE_BOUNDS", {}),
  }


def local_report(s):
  """离线规则诊断 —— 不需要网络、不需要 key、毫秒级完成。"""
  L = []
  add = L.append
  ovr = s["累计干预"]
  total_ovr = sum(ovr.values())
  dl = s["行车日志"]
  th = s["学习器门槛"]
  base = s["基线"]
  off = s["已学偏移"]
  ema = s["净奖励EMA"]
  acc = s["样本累加器"]
  life = s["生命周期样本"]
  safe = s["安全区"]

  add("=" * 58)
  add("  萝卜自动调参 · 体检报告   " + s["生成时间"])
  add("=" * 58)

  # ---------- 1. 数据量 ----------
  add("")
  add("【1】数据量")
  add("  累计干预 %d 次 (踩刹%d / 踩油%d / 弯道%d / 转向%d)"
      % (total_ovr, ovr["踩刹车"], ovr["踩油门"], ovr["弯道"], ovr["转向"]))
  add("  累计调参 %d 次 | 行车日志 %d 条 (%.1f 分钟激活)"
      % (s["累计调参次数"], dl["记录条数"], dl["累计激活分钟"]))
  if total_ovr < 50:
    add("  → 样本偏少, 建议再累积一些里程后再看结论。")
  elif dl["记录条数"] == 0:
    add("  → 行车日志尚未产生 (刚升级后需要先跑一段 onroad)。")
  else:
    add("  → 数据量充足。")

  # ---------- 2. 学习成效 ----------
  add("")
  add("【2】学习成效")
  if not off:
    add("  当前没有任何参数学到偏移。")
  for k, bins in sorted(off.items()):
    nm = KNOB_INFO.get(k, (k, ""))[0]
    dirn = KNOB_INFO.get(k, (k, ""))[1]
    b = base.get(k)
    for ctx, v in bins.items():
      tag = {"s0": "低速", "s1": "中速", "s2": "高速",
             "g0": "缓弯", "g1": "中弯", "g2": "急弯", "": "全局"}.get(ctx, ctx)
      eff = (b + v) if isinstance(b, int) else "?"
      add("  %-22s [%s] 基线%s → %s  (偏移%+d, %s)" % (nm, tag, b, eff, v, dirn))

  # ---------- 3. 采样健康度 ----------
  add("")
  add("【3】采样健康度  (门槛 MIN_SAMPLES=%s, 每 %.0fs 结算一次)"
      % (th.get("MIN_SAMPLES"), float(th.get("ADAPT_INTERVAL_S") or 45)))
  cats = {}
  for k, v in life.items():
    c = ("follow" if k.startswith("TFollow") or k in ("ComfortBrake", "StopDistanceCarrot")
         else "accel" if k.startswith("CruiseMaxVals")
         else "curve" if k.startswith("AutoCurveSpeed")
         else "steer" if k == "LatMpcSteeringRateCost" else "other")
    d = cats.setdefault(c, {"n": 0, "w": 0.0, "keys": 0})
    d["n"] += int(v.get("n", 0))
    d["w"] += float(v.get("w", 0.0))
    d["keys"] += 1
  if not cats:
    add("  尚无生命周期样本统计 (该统计为本次升级新增, 需重启学习器后开始累计)。")
  for c, d in sorted(cats.items(), key=lambda x: -x[1]["n"]):
    add("  %-4s 事件%4d 次 / 累计权重 %.1f / 覆盖 %d 个参数"
        % (CAT_NAME.get(c, c), d["n"], d["w"], d["keys"]))
  if acc:
    add("  正在攒样本的桶 (未达门槛, 会跨周期保留):")
    for k, bins in sorted(acc.items()):
      nm = KNOB_INFO.get(k, (k, ""))[0]
      for ctx, v in bins.items():
        pct = 100.0 * float(v[1]) / max(0.01, float(th.get("MIN_SAMPLES") or 2))
        add("    %-22s [%s] 权重 %.2f  (进度 %.0f%%)" % (nm, ctx or "全局", v[1], min(999, pct)))
  else:
    add("  当前没有正在累积的样本桶。")

  # ---------- 4. 安全边界 ----------
  add("")
  add("【4】手动设定 vs 安全区")
  outs = []
  for k, b in sorted(base.items()):
    rng = safe.get(k)
    if not rng or not isinstance(b, int):
      continue
    lo, hi = rng[0], rng[1]
    if b < lo or b > hi:
      _inf = KNOB_INFO.get(k, (k, "", ""))
      nm, dirn = _inf[0], _inf[1]
      side = "低于下界" if b < lo else "高于上界"
      outs.append("  %-22s = %d, %s (安全区 %d~%d) | %s" % (nm, b, side, lo, hi, dirn))
  if outs:
    add("  以下参数是你手动设到安全区之外的 —— 学习器已被要求尊重这些值, 不再拉回:")
    L.extend(outs)
    add("  (学习器仍被禁止在这些参数上继续朝危险方向学习)")
  else:
    add("  所有基线均在安全区内。")

  # ---------- 5. 结论与建议 ----------
  add("")
  add("【5】诊断结论")
  hints = []
  if total_ovr >= 100 and len(off) <= 1:
    hints.append("干预次数已达 %d 次却几乎没有学到偏移 —— 典型的采样门槛过高症状。"
                 "本次升级已改为累加器跨周期保留 + 门槛降到 %s, 后续应能正常积累。"
                 % (total_ovr, th.get("MIN_SAMPLES")))
  if ovr["踩油门"] > ovr["踩刹车"] * 1.5 and ovr["踩油门"] > 30:
    hints.append("踩油门(%d)明显多于踩刹车(%d): 你普遍嫌系统起步/加速偏肉, "
                 "预期学习方向是加大加速性、缩短跟车距离。" % (ovr["踩油门"], ovr["踩刹车"]))
  elif ovr["踩刹车"] > ovr["踩油门"] * 1.5 and ovr["踩刹车"] > 30:
    hints.append("踩刹车(%d)明显多于踩油门(%d): 你普遍嫌系统跟得太近/太冲, "
                 "预期学习方向是拉长跟车距离、降低加速性。" % (ovr["踩刹车"], ovr["踩油门"]))
  if ovr["弯道"] > 80:
    hints.append("弯道干预 %d 次偏多, 说明过弯速度策略与你的习惯差距较大, "
                 "可重点关注弯道激进度两项参数。" % ovr["弯道"])
  if ovr["转向"] > 150:
    hints.append("转向干预 %d 次很多, 转向手感(LatMpcSteeringRateCost)是当前唯一学到东西的参数, "
                 "方向正确。" % ovr["转向"])
  if not ema:
    hints.append("净奖励 EMA 全空, 说明历史奖励都被衰减清零了 —— 这正是本次修复的目标。")
  if not hints:
    hints.append("未发现异常, 学习器工作正常。")
  for i, h in enumerate(hints, 1):
    add("  %d) %s" % (i, h))

  # ---------- 6. 参数含义速查 ----------
  # 只列本报告真正涉及的参数 (已学到偏移 / 正在攒样本 / 手动超安全区),
  # 不把 16 项全铺开, 避免报告过长。完整说明在车机「自动调参记录」每条记录下也有。
  add("")
  add("【6】参数含义速查 (调大/调小分别什么效果)")
  # 按组聚合: 同组参数只解释一次, 并列出组内实际涉及的具体参数名。
  groups, order = {}, []
  for k in list(off.keys()) + list(acc.keys()) + [o for o in base.keys() if o in safe]:
    gk, gname, gexpl = knob_group(k)
    if not gexpl:
      continue
    if gk not in groups:
      groups[gk] = {"name": gname, "expl": gexpl, "members": []}
      order.append(gk)
    if k not in groups[gk]["members"]:
      groups[gk]["members"].append(k)
  if order:
    for gk in order:
      g = groups[gk]
      mem = sorted(g["members"])
      tag = "%s 等 %d 项" % (mem[0], len(mem)) if len(mem) > 2 else ", ".join(mem)
      add("  * %s  [%s]" % (g["name"], tag))
      add("      %s" % g["expl"])
  else:
    add("  (本次报告未涉及具体参数)")

  add("")
  add("=" * 58)
  return "\n".join(L)


def _ordered_providers():
  """按缓存优先返回候选平台: 上次成功的排第一, 其余保持默认优先级。"""
  cached = ""
  try:
    if os.path.exists(PROVIDER_CACHE_PATH):
      cached = open(PROVIDER_CACHE_PATH).read().strip()
  except Exception:
    cached = ""
  if not cached:
    return list(AI_PROVIDERS)
  head = [p for p in AI_PROVIDERS if p[0] == cached]
  return head + [p for p in AI_PROVIDERS if p[0] != cached]


def _post_chat(endpoint, model, key, prompt, extra=None, timeout=None):
  """向一个 OpenAI 兼容端点发一次 chat 请求, 返回正文字符串。失败抛异常。

  extra: 平台专属请求体参数 (如硅基流动 Qwen3 需 enable_thinking=False 关闭深度推理,
         否则模型进入长思考、生成极慢导致连接超时)。
  timeout: 单次请求超时秒数, 缺省用 AI_TIMEOUT。建议生成要输出多条 JSON, 需要更长时间。
  """
  payload = {
    "model": model,
    "messages": [{"role": "user", "content": prompt}],
    "temperature": 0.3,
  }
  if extra:
    payload.update(extra)
  body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
  req = urllib.request.Request(
    endpoint, data=body,
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
  with urllib.request.urlopen(req, timeout=(timeout or AI_TIMEOUT)) as resp:
    data = json.loads(resp.read().decode("utf-8"))
  return data["choices"][0]["message"]["content"].strip()


def ai_report(summary, key, local_text=""):
  """把摘要发给免费云端 LLM 做自然语言深度分析 (仅上传几 KB JSON, 无 rlog/视频/定位)。

  多平台自动探测: 依次尝试 AI_PROVIDERS 里的端点, 第一个鉴权通过的即采用,
  并把平台名缓存到 PROVIDER_CACHE_PATH, 下次直接命中、不再逐个重试。
  返回 (正文, 平台名, 模型名); 全部失败时抛 RuntimeError 并附各家错误原因。
  """
  payload = json.dumps(summary, ensure_ascii=False)
  if len(payload) > 20000:
    payload = payload[:20000] + "...(截断)"

  # 关键语义提示: 小参数量模型无法自行从原始计数推出干预方向的含义,
  # 不喂这段会得出"踩油门多 = 偏好保守"这类自相矛盾的结论。
  semantics = (
    "【干预语义, 必须据此判断偏好, 不要弄反】\n"
    "- 踩油门(accel)次数多 = 车主嫌系统起步/加速太肉 => 偏好更激进 "
    "=> 应加大加速性(CruiseMaxVals*)、缩短跟车距离(TFollowGap* 调小)。\n"
    "- 踩刹车(brake)次数多 = 车主嫌系统跟太近/减速太晚 => 偏好更保守 "
    "=> 应加大跟车距离(TFollowGap* 调大)、增强舒适制动。\n"
    "- 弯道(curve)干预多 = 过弯速度策略与习惯不符, 看方向定激进度增减。\n"
    "- 转向(steer)干预多 = 横向手感不合, LatMpcSteeringRateCost 大=平顺/迟钝, 小=灵敏。\n"
    "- TFollowGap 数值越大 = 跟车越远越保守。\n"
  )

  local_block = ""
  if local_text:
    lt = local_text if len(local_text) <= 6000 else local_text[:6000] + "...(截断)"
    local_block = ("\n【设备本地已算出的分析结论, 请在此基础上深化, 不要与之矛盾】\n"
                   + lt + "\n")

  prompt = (
    "你是 openpilot / carrotpilot 自动驾驶纵横向调参专家。下面是一台 comma 2 设备上"
    "自研参数学习器的运行状态摘要(JSON), 以及设备本地已生成的分析结论。\n\n"
    + semantics + local_block +
    "\n请用简体中文给出:\n"
    "1) 这位车主的驾驶偏好画像(激进/保守, 更在意跟车距离还是加速性);\n"
    "2) 学习器当前是否健康, 若参数没学动请指出最可能的原因;\n"
    "3) 3~5 条具体可执行的调参建议, 每条注明参数名、建议方向与幅度、理由 "
    "(各条之间方向必须自洽, 不能既让跟车更远又让加速更猛);\n"
    "4) 任何你发现的安全隐患。\n"
    "要求: 直接给结论, 不要复述 JSON, 不要客套, 总长度控制在 600 字以内。\n\n"
    "```json\n" + payload + "\n```"
  )
  errors = []
  for name, endpoint, model, _note, extra in _ordered_providers():
    try:
      txt = _post_chat(endpoint, model, key, prompt, extra)
      try:      # 命中即缓存, 下次直连
        with open(PROVIDER_CACHE_PATH, "w") as f:
          f.write(name)
      except Exception:
        pass
      return txt, name, model
    except urllib.error.HTTPError as e:
      detail = ""
      try:    detail = e.read().decode("utf-8", "replace")[:160]
      except Exception: pass
      errors.append("%s: HTTP %s %s" % (name, e.code, detail))
    except Exception as e:
      errors.append("%s: %s" % (name, type(e).__name__ + " " + str(e)[:120]))

  raise RuntimeError("所有平台均失败 ->\n    " + "\n    ".join(errors))


# ============================================================================
#  AI 结构化调参建议 (与学习器记录彻底分离的第二条链路)
#
#  设计红线:
#    1. AI 输出一律视为不可信输入, 必须过四道闸门: 白名单 / 安全区 / 单次幅度 / 条数上限;
#    2. 建议只落盘到 carrot_ai_suggestions.json, 绝不写 carrot_learn_changes.json;
#    3. 本文件永远不写任何真实参数 —— 只有用户在车机上逐条勾选后, 才由 learner 应用;
#    4. 与报告分两次调用: 小参数量模型很难在一次回复里既写好散文又保持 JSON 不破格式。
# ============================================================================

def _provider_by_name(name):
  for it in AI_PROVIDERS:
    if it[0] == name:
      return it
  return None


def current_knob_values():
  """读取全部白名单参数的当前实际值 —— 建议幅度必须以真实当前值为基准, 不能用基线。"""
  cur = {}
  try:
    sys.path.insert(0, "/data/openpilot")
    from common.params import Params
    p = Params()
    for k in _KNOB_SPECS.keys():
      try:
        v = p.get(k)
        if v is None:
          continue
        cur[k] = int(round(float(v.decode() if isinstance(v, bytes) else v)))
      except Exception:
        continue
  except Exception:
    pass
  return cur


def _eff_range(k, c):
  """有效允许区间 = 安全区, 但被当前值撑开 (与学习器 _eff_bounds 同一规则: 尊重用户手动值)。"""
  spec = _KNOB_SPECS.get(k, {})
  lo, hi = _SAFE_BOUNDS.get(k, (spec.get("lo", c), spec.get("hi", c)))
  return min(lo, c), max(hi, c)


def validate_suggestions(items, cur):
  """四道闸门过滤 AI 建议。返回 (通过列表, 被拒原因列表)。

  超幅度的不直接丢, 而是裁剪到上限 —— AI 方向判断通常对, 错的往往只是步子迈太大。
  """
  ok, rej, seen = [], [], set()
  if not isinstance(items, list):
    return ok, ["AI 返回的不是数组"]
  for it in items:
    if not isinstance(it, dict):
      rej.append("非法条目(不是对象)")
      continue
    k = str(it.get("param", "")).strip()
    if k not in _KNOB_SPECS:
      rej.append("%s: 不在可调白名单" % (k or "?"))
      continue
    if k in seen:
      rej.append("%s: 同一参数重复建议" % k)
      continue
    if k not in cur:
      rej.append("%s: 读不到当前值" % k)
      continue
    try:
      nv = int(round(float(it.get("new"))))
    except Exception:
      rej.append("%s: 新值不是数字" % k)
      continue
    c = cur[k]
    if nv == c:
      rej.append("%s: 与当前值相同" % k)
      continue
    lo, hi = _eff_range(k, c)
    if nv < lo or nv > hi:
      # 收到安全区边界而非直接丢弃: 边界本身就是安全的, AI 常常只是超了一两个点,
      # 为此白白废掉一条方向正确的建议不划算。
      clipped = min(max(nv, lo), hi)
      rej.append("%s: %d 超出安全区 [%d,%d], 已收到边界 %d" % (k, nv, lo, hi, clipped))
      nv = clipped
      if nv == c:
        continue
    cap = max(1, int(round(abs(c) * AI_MAX_STEP_RATIO)))
    if abs(nv - c) > cap:
      clipped = c + (cap if nv > c else -cap)
      rej.append("%s: 幅度 %+d 超单次上限 ±%d, 已裁剪为 %+d" % (k, nv - c, cap, clipped - c))
      nv = clipped
      if nv == c or nv < lo or nv > hi:
        continue
    seen.add(k)
    ok.append({
      "param":   k,
      "label":   KNOB_INFO.get(k, (k,))[0],
      "cur":     c,
      "new":     nv,
      "delta":   nv - c,
      "lo":      lo,
      "hi":      hi,
      "reason":  str(it.get("reason", "")).strip()[:140],
      "explain": knob_explain(k),
      "applied": False,
    })
    if len(ok) >= AI_MAX_ITEMS:
      break
  return ok, rej


def _extract_json_array(txt):
  """从模型回复里抠出 JSON 数组 —— 小模型常给 ```json 包裹或前后带废话。"""
  if not txt:
    return None
  t = txt.strip()
  m = re.search(r"```(?:json)?\s*(.+?)\s*```", t, re.S)
  if m:
    t = m.group(1).strip()
  i, j = t.find("["), t.rfind("]")
  if i < 0 or j <= i:
    return None
  try:
    return json.loads(t[i:j + 1])
  except Exception:
    return None


def ai_suggest(summary, key, cur, provider_name="", local_text=""):
  """独立第二次调用: 只要结构化 JSON 建议。返回 (原始条目列表, 平台名, 模型名)。"""
  if not cur:
    raise RuntimeError("读不到任何参数当前值")

  knob_lines = []
  for k in sorted(cur.keys()):
    c = cur[k]
    lo, hi = _eff_range(k, c)
    cap = max(1, int(round(abs(c) * AI_MAX_STEP_RATIO)))
    inf = KNOB_INFO.get(k, (k, "", ""))
    knob_lines.append("  %-30s 当前=%-5d 允许=[%d,%d] 本次最多变动=±%d   // %s, %s"
                      % (k, c, lo, hi, cap, inf[0], inf[1]))

  ctx = json.dumps({
    "累计干预": summary.get("累计干预", {}),
    "行车日志": summary.get("行车日志", {}),
    "已学偏移": summary.get("已学偏移", {}),
    "最近调参记录": summary.get("最近调参记录", [])[-6:],
  }, ensure_ascii=False)
  if len(ctx) > 4000:
    ctx = ctx[:4000] + "...(截断)"

  prompt = (
    "你是 openpilot/carrotpilot 调参专家。根据下面这台车的驾驶数据, 给出具体的参数调整建议。\n\n"
    "【干预语义, 不要判反】\n"
    "- 踩油门多 = 嫌系统太肉 => 加速性(CruiseMaxVals*)调大 / 跟车时距(TFollowGap*)调小;\n"
    "- 踩刹车多 = 嫌系统跟太近或减速太晚 => 跟车时距调大 / 舒适制动(ComfortBrake)调大;\n"
    "- TFollowGap 数值越大跟车越远越保守; LatMpcSteeringRateCost 大=平顺迟钝, 小=灵敏。\n\n"
    "【可调参数与硬性约束 (必须遵守, 超出的建议会被程序直接丢弃)】\n"
    + "\n".join(knob_lines) + "\n\n"
    "【驾驶数据】\n" + ctx + "\n\n"
    "【输出要求 —— 极其重要】\n"
    "只输出一个 JSON 数组, 不要任何解释文字、不要 markdown 标题、不要客套。\n"
    "请给出 3~%d 条建议, 覆盖跟车 / 加速 / 弯道 / 转向等不同方面, 不要只给一条。格式示例(仅示范格式, 数值以实际数据为准):\n"
    '[\n'
    '  {"param":"CruiseMaxVals1","new":170,"reason":"油门干预偏多, 中低速加速偏肉"},\n'
    '  {"param":"TFollowGap2","new":114,"reason":"偏好激进, 标准档跟车可略近"},\n'
    '  {"param":"LatMpcSteeringRateCost","new":4,"reason":"转向干预多, 手感偏迟钝"}\n'
    ']\n'
    "规则: new 必须是整数且落在该参数的允许区间内; 变动量不得超过该参数标注的本次最多变动;\n"
    "最多 %d 条; 各条方向必须自洽(不能既让跟车更远又让加速更猛); reason 用简体中文, 30 字以内;\n"
    "如果数据确实不足以支撑任何调整, 才输出空数组 []。\n" % (AI_MAX_ITEMS, AI_MAX_ITEMS)
  )

  order = []
  hit = _provider_by_name(provider_name)
  if hit:
    order.append(hit)                      # 报告已探出可用平台, 直接复用, 不再逐个重试
  for it in _ordered_providers():
    if it not in order:
      order.append(it)

  errors = []
  for name, endpoint, model, _note, extra in order:
    try:
      # 输出多条 JSON 比写一段散文慢, 单独放宽超时; 同时限制 max_tokens 防止模型啰嗦跑飞。
      ex = dict(extra or {})
      ex.setdefault("max_tokens", 900)
      txt = _post_chat(endpoint, model, key, prompt, ex, timeout=AI_SUGGEST_TIMEOUT)
      arr = _extract_json_array(txt)
      if arr is None:
        errors.append("%s: 回复中找不到 JSON 数组" % name)
        continue
      return arr, name, model
    except Exception as e:
      errors.append("%s: %s" % (name, type(e).__name__ + " " + str(e)[:100]))
  raise RuntimeError("; ".join(errors))


def save_suggestions(items, provider, model, rejected):
  """落盘 AI 建议候选。保留上一批里已应用条目的状态, 避免刷新后丢失'已应用'标记。"""
  data = {
    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
    "provider": provider,
    "model": model,
    "items": [dict(it, id=i + 1) for i, it in enumerate(items)],
    "rejected": rejected[:12],
  }
  tmp = SUGGEST_PATH + ".tmp"
  with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
  os.replace(tmp, SUGGEST_PATH)
  return data


def main():
  args = sys.argv[1:]
  s = build_summary()

  if "--summary" in args:
    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0

  rep = local_report(s)

  if "--ai" in args:
    key = ""
    try:
      # 优先读菜单内配置的 Param(CarrotAiCloudKey), 回退到文件 /data/carrot_ai_key.txt
      sys.path.insert(0, "/data/openpilot")
      from common.params import Params
      _k = Params().get("CarrotAiCloudKey")
      if _k:
        key = _k.decode().strip() if isinstance(_k, bytes) else str(_k).strip()
      if not key and os.path.exists(KEY_PATH):
        key = open(KEY_PATH).read().strip()
    except Exception as e:
      key = ""
      print("[ai] key load failed:", e)
    if not key:
      rep += ("\n[云端 AI] 未配置 API key, 已跳过 (本地分析不受影响)。\n"
              "  配置方式: 车机「设置 → 功能 → 萝卜菜单 → 驾驶习惯自学习 → 设置云端AI密钥」\n"
              "  或写入文件 %s。推荐硅基流动 cloud.siliconflow.cn (永久免费, 手机号注册)。\n" % KEY_PATH)
    else:
      try:
        t0 = time.time()
        txt, prov, model = ai_report(s, key, rep)
        rep += ("\n\n" + "=" * 58
                + "\n  云端 AI 深度分析 (%s / %s, 耗时 %.1fs)\n" % (prov, model, time.time() - t0)
                + "=" * 58 + "\n" + txt + "\n")

        # --- 结构化调参建议: 独立第二次调用, 失败绝不影响上面的报告正文 ---
        try:
          cur = current_knob_values()
          raw, sprov, smodel = ai_suggest(s, key, cur, prov, txt)
          sug, rej = validate_suggestions(raw, cur)
          save_suggestions(sug, sprov, smodel, rej)
          rep += ("\n" + "-" * 58 + "\n  AI 调参建议: 已生成 %d 条候选\n" % len(sug) + "-" * 58 + "\n")
          if sug:
            for i, it in enumerate(sug, 1):
              rep += ("  %d) %s (%s)  %d -> %d  (%+d)\n      理由: %s\n"
                      % (i, it["label"], it["param"], it["cur"], it["new"], it["delta"],
                         it["reason"] or "(未给出)"))
            rep += ("\n  这些建议不会自动生效。到车机「设置 → 功能 → AI 调参建议」里\n"
                    "  逐条勾选后点[应用所选]才写入, 且与学习器记录分开存放、可随时撤销。\n")
          else:
            rep += "  (本次没有通过校验的建议 —— 数据不足或 AI 给的值都不合规)\n"
          if rej:
            rep += ("  AI 原始输出有 %d 处问题, 已拦截或自动修正: %s\n"
                    % (len(rej), " | ".join(rej[:4])))
        except Exception as e:
          rep += "\n[AI 调参建议] 生成失败(不影响以上报告): %s\n" % e
      except Exception as e:
        rep += ("\n[云端 AI] 调用失败, 已保留本地分析结果。\n  %s\n"
                "  说明: 已自动尝试全部候选平台(硅基流动/智谱/火山/阿里), 均未通过鉴权。\n"
                "  多为 key 无效或平台未开通。推荐换硅基流动 cloud.siliconflow.cn:\n"
                "  手机号注册 → API 密钥 → 新建 → 粘到车机「设置云端AI密钥」即可(永久免费)。\n" % e)

  print(rep)
  try:
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
      f.write(rep + "\n")
    print("\n报告已保存: %s" % REPORT_PATH)
  except Exception as e:
    print("报告写入失败: %s" % e)

  # 轮转: 归档历史报告, 仅保留最近 10 份, 避免磁盘占满
  try:
    rep_dir = os.path.join(HERE, "reports")
    os.makedirs(rep_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    with open(os.path.join(rep_dir, "report_%s.txt" % ts), "w", encoding="utf-8") as af:
      af.write(rep + "\n")
    arcs = sorted(glob.glob(os.path.join(rep_dir, "report_*.txt")))
    while len(arcs) > 10:
      try: os.remove(arcs[0])
      except OSError: pass
      arcs = arcs[1:]
    print("历史报告已归档(保留最近 %d 份): %s" % (len(arcs), rep_dir))
  except Exception as e:
    print("报告归档失败(不影响主报告): %s" % e)
  return 0


if __name__ == "__main__":
  sys.exit(main())
