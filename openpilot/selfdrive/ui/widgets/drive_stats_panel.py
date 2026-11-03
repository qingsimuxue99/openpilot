"""驾驶数据面板（offroad 待机主页用）

移植自旧版 Qt 的 ``selfdrive/ui/qt/widgets/drive_stats_panel.cc``
（DriveStatsPanel），在当前纯 Python raylib UI 中用等价方式重绘：

* 标题「驾驶数据」
* 上排三张里程大卡：今日 / 本周 / 累计（km + 「行程 N 次 · 时长 M 分」）
* 下排左侧「最近 7 天里程 (km)」柱状图（最多 7 根，橙色柱，柱顶数值 + 柱底 MM/DD）
* 下排右侧「系统状态」四宫格：CPU 温度 / 内存可用 / 存储剩余 / 运行时间

数据源：``/data/drive_stats/drive_stats.json``，由
``openpilot/selfdrive/carrot/drive_stats.py``（manager 托管的常驻进程）
按 carState 的 vEgo 累计写入。本面板只读不写。

布局按父容器尺寸等比缩放，因此既可作整页（左栏），也可作局部区域。
"""

from __future__ import annotations

import json
import os
import time

import pyray as rl

from openpilot.system.ui.lib.application import FontWeight
from openpilot.system.ui.lib.text_draw import draw_text_ui_style
from openpilot.system.ui.widgets import Widget

STATS_FILE = "/data/drive_stats/drive_stats.json"
SYS_REFRESH_SEC = 5.0
JSON_CACHE_REFRESH_SEC = 1.0

# 配色（与旧版 Qt 样式表一致）
C_ORANGE = rl.Color(255, 154, 0, 255)
C_WHITE = rl.Color(255, 255, 255, 255)
C_GRAY = rl.Color(170, 170, 170, 255)
C_LGRAY = rl.Color(204, 204, 204, 255)
C_CARD = rl.Color(30, 30, 30, 235)
C_HERO = rl.Color(22, 22, 22, 240)
C_TILE = rl.Color(26, 26, 26, 240)
C_PLOT_BG = rl.Color(42, 42, 42, 255)


def _read_text(path: str) -> str:
  try:
    with open(path) as f:
      return f.read().strip()
  except OSError:
    return ""


class DriveStatsPanel(Widget):
  """「驾驶数据」面板：里程三卡 + 近 7 日柱状图 + 系统状态四宫格。"""

  def __init__(self):
    super().__init__()
    self._stats = None
    self._stats_mtime = 0.0
    self._stats_check_time = 0.0
    self._sys = {}
    self._sys_time = 0.0

  # ------------------------------------------------------------------ 数据

  def _read_stats(self):
    """读取累计数据（按 mtime 缓存，最多每 1 秒探一次盘）。"""
    now = time.monotonic()
    if now - self._stats_check_time < JSON_CACHE_REFRESH_SEC:
      return self._stats
    self._stats_check_time = now
    try:
      mtime = os.path.getmtime(STATS_FILE)
    except OSError:
      return self._stats
    if mtime != self._stats_mtime:
      try:
        with open(STATS_FILE, encoding="utf-8") as f:
          self._stats = json.load(f)
        self._stats_mtime = mtime
      except Exception:
        pass
    return self._stats

  def _read_system(self) -> dict:
    """系统状态：CPU 温度 / 内存可用 / 存储剩余 / 运行时间（5 秒刷新）。"""
    now = time.monotonic()
    if now - self._sys_time < SYS_REFRESH_SEC and self._sys:
      return self._sys
    self._sys_time = now
    out = {}

    t = _read_text("/sys/class/thermal/thermal_zone0/temp")
    if t:
      try:
        out["cpu"] = f"{int(t) / 1000:.0f} °C"
      except ValueError:
        pass

    mt = ma = 0.0
    for line in _read_text("/proc/meminfo").split("\n"):
      if line.startswith("MemTotal:"):
        mt = float(line.split()[1])
      elif line.startswith("MemAvailable:"):
        ma = float(line.split()[1])
    if mt > 0:
      out["mem"] = f"{int(ma / mt * 100)} %"

    try:
      st = os.statvfs("/data")
      out["store"] = f"{st.f_bavail * st.f_frsize / 1024.0 / 1024.0 / 1024.0:.1f} GB"
    except OSError:
      pass

    up = _read_text("/proc/uptime")
    if up:
      try:
        secs = float(up.split()[0])
        d = int(secs // 86400)
        secs -= d * 86400
        h = int(secs // 3600)
        secs -= h * 3600
        out["uptime"] = f"{d}天{h}时{int(secs // 60)}分"
      except (ValueError, IndexError):
        pass

    if out:
      self._sys = out
    return self._sys

  # ------------------------------------------------------------------ 绘制

  def _render(self, rect: rl.Rectangle):
    data = self._read_stats()
    if data is None:
      draw_text_ui_style("暂无数据", rect.x + rect.width // 2, rect.y + rect.height // 2,
                         int(rect.height * 0.07), C_GRAY, align="center", y_offset=0.0)
      return

    # ★ 全部尺寸按「高度」为基准：面板会随窗口宽度变化拉宽，若按宽度算字号，
    #   宽屏下会字号巨大、三卡被挤、下排图表被压扁。
    # ⚠️ draw_text_ui_style 的 y 是文字**基线**，中文字形实际向上延伸明显超过
    #   0.85 个字号。标题上下都要留足余量，否则上边被 header 压、下边被卡片盖。
    #   这里标题用独立的上下留白（title_pad_top / title_gap），不与卡片的 pad 共用。
    H = rect.height
    W = rect.width
    pad = int(min(W, H) * 0.040)
    fs_title = int(min(H * 0.046, 40))

    # 标题上下都留足余量：中文字形实际渲染高度远超 0.85/0.22 估算。
    #   · title_pad_top 加大 ⇒ 标题不再贴着面板上沿
    #   · title_gap  加大 ⇒ 标题不被下方卡片压
    # 标题下方留白：⚠️ draw_text_ui_style 默认 border_width=3 且 shadow_offset=8
    #   （阴影向**下**延伸），所以字形实际占位比 y 大 ~11px，此前按 0.85/0.22 估算
    #   一直偏小 ⇒ 三卡顶到标题。这里把描边+阴影计入。
    TEXT_OUTLINE_PX = 12
    title_pad_top = int(fs_title * 1.15)
    title_gap = int(fs_title * 1.20) + TEXT_OUTLINE_PX

    title_base = rect.y + title_pad_top + fs_title
    draw_text_ui_style("驾驶数据", rect.x + pad, title_base, fs_title, C_WHITE,
                       align="left", y_offset=0.0)

    top_y = title_base + title_gap
    avail_h = rect.y + H - pad - top_y

    # ---------- 上排三张大卡 ----------
    hero_h = int(avail_h * 0.38)
    hero_gap = int(min(W, H) * 0.026)
    hero_w = (W - pad * 2 - hero_gap * 2) // 3
    inner_x = int(hero_w * 0.06)      # 卡内左右留白，防文字贴边

    metric = self._is_metric()
    for i, (name, key) in enumerate((("今日", "today"), ("本周", "week"), ("累计", "total"))):
      hx = rect.x + pad + i * (hero_w + hero_gap)
      rl.draw_rectangle_rounded(rl.Rectangle(hx, top_y, hero_w, hero_h), 0.06, 14, C_HERO)

      b = data.get(key) or {}
      km = float(b.get("km", 0.0) or 0.0)
      trips = int(b.get("trips", 0) or 0)
      mins = float(b.get("min", 0.0) or 0.0)
      km_disp = km if metric else km * 0.621371
      unit = "km" if metric else "mi"

      # 字号按卡高算并设绝对上限；用户反馈偏小，故整体上调一档
      fs_name = max(20, min(int(hero_h * 0.165), 36))
      fs_km = max(30, min(int(hero_h * 0.26), int(hero_w * 0.19), 64))
      fs_unit = max(17, min(int(hero_h * 0.135), 30))
      fs_sub = max(16, min(int(hero_h * 0.112), 27))

      cx = hx + hero_w // 2
      cy = top_y + int(hero_h * 0.22)
      draw_text_ui_style(name, cx, cy, fs_name, C_GRAY, align="center", y_offset=0.0)
      cy += int(fs_km * 1.05)
      draw_text_ui_style(f"{km_disp:.1f}", cx, cy, fs_km, C_ORANGE, align="center", y_offset=0.0)
      cy += int(fs_km * 0.66)
      draw_text_ui_style(unit, cx, cy, fs_unit, C_ORANGE, align="center", y_offset=0.0)
      cy += int(fs_unit * 1.70)
      draw_text_ui_style(f"行程 {trips} 次 · 时长 {mins:.0f} 分", cx, cy, fs_sub, C_LGRAY,
                         align="center", y_offset=0.0)

    # ---------- 下排：柱状图 + 系统状态 ----------
    bot_y = top_y + hero_h + int(avail_h * 0.045)
    bot_h = rect.y + H - pad - bot_y
    col_gap = int(min(W, H) * 0.026)
    sys_w = int((W - pad * 2 - col_gap) * 0.34)   # 系统状态略窄，把宽度让给柱状图
    chart_w = W - pad * 2 - col_gap - sys_w
    chart_x = rect.x + pad
    sys_x = chart_x + chart_w + col_gap

    rl.draw_rectangle_rounded(rl.Rectangle(chart_x, bot_y, chart_w, bot_h), 0.05, 16, C_CARD)
    rl.draw_rectangle_rounded(rl.Rectangle(sys_x, bot_y, sys_w, bot_h), 0.05, 16, C_CARD)

    fs_card = max(19, int(bot_h * 0.105))
    # 卡片标题基线留出描边+阴影（向下 11px），否则会压到绘图区/数值区
    card_title_y = bot_y + int(bot_h * 0.13) + 12
    draw_text_ui_style("最近 7 天里程 (km)", chart_x + int(pad * 0.8), card_title_y, fs_card,
                       C_WHITE, align="left", y_offset=0.0)
    draw_text_ui_style("系统状态", sys_x + int(pad * 0.8), card_title_y, fs_card,
                       C_WHITE, align="left", y_offset=0.0)

    self._render_chart(data, chart_x, bot_y, chart_w, bot_h, pad)
    self._render_system(sys_x, bot_y, sys_w, bot_h, pad, bot_h)

  def _render_chart(self, data, x, y, w, h, pad):
    daily = data.get("daily") or {}
    # ⚠️ 补齐「今天」：drive_stats.json 的 daily 只在有里程时才写入当天键，
    #   所以今天若还没开车，图中就少了今天这一列（实机出现过只剩 10/10~10/16）。
    #   这里以最近 7 天为基准补齐缺失日期，缺数据按 0 显示。
    try:
      import datetime
      today = datetime.date.today()
      # 以 daily 里最后一天为锚点向前推 7 天；若最后一天早于今天，则延伸到今天
      anchor = max([datetime.date.fromisoformat(k) for k in daily.keys()], default=today - datetime.timedelta(days=6))
      if anchor < today:
        anchor = today
      span = [(anchor - datetime.timedelta(days=i)) for i in range(6, -1, -1)]
      days = [d.isoformat() for d in span]
      values = [float(daily.get(k, 0.0) or 0.0) for k in days]
    except Exception:
      days = sorted(daily.keys())[-7:]
      values = [float(daily[k]) for k in days]
    fs_v = max(15, min(int(h * 0.075), int(w * 0.030)))
    fs_d = max(14, min(int(h * 0.070), int(w * 0.030)))

    # ⚠️ 三段垂直分配（保证柱顶数值永远落在柱子上方的预留区内）：
    #   card_title_h 下排卡片标题 / value_h 柱顶数值预留 / date_h 柱底日期
    #   ⚠️ draw_text_ui_style 默认带描边 3px + 阴影偏移 8px（向下），
    #      所以 value_h 必须 > 字号，否则数值字形(+阴影)会压到柱子上。
    card_title_h = int(h * 0.26)
    value_h = int(fs_v * 3.0)        # 数值行：字号 + 描边 + 8px 向下阴影 + 上移余量
    date_h = int(fs_d * 2.4)         # 日期行
    plot_top = y + card_title_h + value_h
    plot_bot = y + h - date_h
    plot_h = max(20, int((plot_bot - plot_top) * 0.80))
    plot_bot = plot_top + plot_h

    if not days:
      draw_text_ui_style("暂无数据", x + w // 2, (plot_top + plot_bot) // 2,
                         max(18, int(h * 0.10)), C_GRAY, align="center", y_offset=0.0)
      return

    maxv = max(values + [0.0001])
    n = len(days)
    inner_w = w - pad * 2
    scale = min(1.0, inner_w / (100 * n + 34 * (n - 1)))
    bw = max(24, int(100 * scale))
    bgap = max(8, int(34 * scale))
    total_w = bw * n + bgap * (n - 1)
    start_x = x + (w - total_w) // 2

    # ⚠️ 柱顶数值：draw_text_ui_style 有 3px 描边 + **8px 向下阴影**，
    #   字形实际底边 ≈ y + size*0.22 + 11。要让「阴影都不碰柱顶」，
    #   基线必须再上移一个 size + 阴影余量。
    val_baseline = plot_top - int(fs_v * 0.55) - 14
    for i, d in enumerate(days):
      v = values[i]
      bx = start_x + i * (bw + bgap)
      bh = max(3, int(v / maxv * plot_h))
      # 背景柱（轨道）
      rl.draw_rectangle_rounded(rl.Rectangle(bx, plot_top, bw, plot_h), 0.06, 6, C_PLOT_BG)
      # 数值柱
      rl.draw_rectangle_rounded(rl.Rectangle(bx, plot_bot - bh, bw, bh), 0.12, 6, C_ORANGE)
      # 柱顶数值（固定画在柱区上方，不随柱高浮动，避免被高柱压住）
      draw_text_ui_style(f"{v:.1f}", bx + bw // 2, val_baseline,
                         fs_v, C_WHITE, align="center", y_offset=0.0)
      # 柱底日期
      draw_text_ui_style(d[5:].replace("-", "/"), bx + bw // 2, plot_bot + int(date_h * 0.75),
                         fs_d, C_GRAY, align="center", y_offset=0.0)

  def _render_system(self, x, y, w, h, pad, base_h):
    sysd = self._read_system()
    tile_pad = int(w * 0.055)
    tile_gap = int(w * 0.055)
    ty0 = y + int(h * 0.30)
    tw = (w - tile_pad * 2 - tile_gap) // 2
    th = (h - int(h * 0.30) - tile_pad * 2) // 2
    fs_k = max(14, min(int(base_h * 0.075), int(tw * 0.20)))
    fs_v = max(16, min(int(base_h * 0.098), int(tw * 0.26)))

    for i, (k, label) in enumerate((("cpu", "CPU 温度"), ("mem", "内存可用"),
                                    ("store", "存储剩余"), ("uptime", "运行时间"))):
      tx = x + tile_pad + (i % 2) * (tw + tile_gap)
      ty = ty0 + (i // 2) * (th + tile_pad)
      rl.draw_rectangle_rounded(rl.Rectangle(tx, ty, tw, th), 0.09, 10, C_TILE)
      draw_text_ui_style(label, tx + tw // 2, ty + int(th * 0.34), fs_k, C_GRAY,
                         align="center", y_offset=0.0)
      draw_text_ui_style(sysd.get(k, "--"), tx + tw // 2, ty + int(th * 0.74), fs_v, C_ORANGE,
                         align="center", y_offset=0.0)

  @staticmethod
  def _is_metric() -> bool:
    try:
      from openpilot.selfdrive.ui.ui_state import ui_state
      return bool(ui_state.is_metric)
    except Exception:
      return True
