import pyray as rl
import time
import os
import subprocess
from dataclasses import dataclass
from collections.abc import Callable
from openpilot.cereal import log
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos, FONT_SCALE
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget, DialogResult

SIDEBAR_WIDTH = 300
METRIC_HEIGHT = 126
METRIC_WIDTH = 240
METRIC_MARGIN = 30
FONT_SIZE = 35

SETTINGS_BTN = rl.Rectangle(50, 35, 200, 117)
HOME_BTN = rl.Rectangle(60, 860, 180, 180)

# 双击判定间隔（秒）。框架无原生双击事件，自行按「同一张卡的两次点击间隔」判定。
DOUBLE_CLICK_INTERVAL = 0.4

# 车辆连接卡片去抖动（秒）
# 背景：ui_state.py:161 在 pandaStates 断流超过 5*get_fps() 帧时把 panda_type 置为
# unknown，而该阈值随帧率浮动 —— 黑屏唤醒时 UI 降帧，阈值被放大到几百毫秒以上，
# 于是唤醒瞬间必然闪一次「NO PANDA」。这里用固定秒数的宽限期屏蔽这种瞬时抖动。
PANDA_OFF_GRACE = 3.0     # 断流持续超过该秒数才显示 NO PANDA
PANDA_ON_CONFIRM = 0.5    # 恢复在线需连续保持该秒数，避免来回跳

ThermalStatus = log.DeviceState.ThermalStatus
NetworkType = log.DeviceState.NetworkType


# Color scheme
class Colors:
  WHITE = rl.WHITE
  WHITE_DIM = rl.Color(255, 255, 255, 85)
  GRAY = rl.Color(84, 84, 84, 255)

  # Status colors
  GOOD = rl.WHITE
  WARNING = rl.Color(218, 202, 37, 255)
  DANGER = rl.Color(201, 34, 49, 255)

  # UI elements
  METRIC_BORDER = rl.Color(255, 255, 255, 85)
  BUTTON_NORMAL = rl.WHITE
  BUTTON_PRESSED = rl.Color(255, 255, 255, 166)


NETWORK_TYPES = {
  NetworkType.none: tr_noop("--"),
  NetworkType.wifi: tr_noop("Wi-Fi"),
  NetworkType.ethernet: tr_noop("ETH"),
  NetworkType.cell2G: tr_noop("2G"),
  NetworkType.cell3G: tr_noop("3G"),
  NetworkType.cell4G: tr_noop("LTE"),
  NetworkType.cell5G: tr_noop("5G"),
}


@dataclass(slots=True)
class MetricData:
  label: str
  value: str
  color: rl.Color

  def update(self, label: str, value: str, color: rl.Color):
    self.label = label
    self.value = value
    self.color = color


class Sidebar(Widget):
  def __init__(self):
    super().__init__()
    self._net_type = NETWORK_TYPES.get(NetworkType.none)
    self._net_strength = 0

    self._temp_status = MetricData(tr_noop("TEMP"), tr_noop("GOOD"), Colors.GOOD)
    self._panda_status = MetricData(tr_noop("VEHICLE"), tr_noop("ONLINE"), Colors.GOOD)
    self._connect_status = MetricData(tr_noop("CONNECT"), tr_noop("OFFLINE"), Colors.WARNING)
    self._recording_audio = False

    # ===== 侧栏系统标签卡片（移植自旧版 Qt sidebar.cc）=====
    # 图中四张卡：温度+CPU / 车辆连接 / 内存 / 风扇+电压。
    # 每张卡两行文字，带底色 + 圆角边框，按阈值变色（温度/内存）。
    self._ip_addr = "--"
    self._cpu_temp = 0.0
    self._cpu_usage = 0.0
    self._mem_usage = 0.0
    self._mem_total_gb = 0.0
    self._voltage = 0.0
    self._fan_rpm = 0
    self._panda_online = False
    # 车辆连接去抖动：ui_state.py 在 pandaStates 断流超过 `5 * get_fps()` 帧时会把
    # panda_type 重置为 unknown（黑屏/唤醒瞬间 UI 降帧，阈值被同步放大 ⇒ 必然触发）。
    # 这里不改动全局 ui_state（影响面大），而是本卡片自行加宽限期与滞回：
    #   · 断流不足 PANDA_OFF_GRACE 秒 → 维持上一状态（不闪）
    #   · 连续在线 PANDA_ON_CONFIRM 秒   → 才认定恢复
    self._panda_display_online = False
    self._panda_last_seen = 0.0
    self._panda_off_since = 0.0
    self._fan_full = False
    # 双击命中区（切换风扇 / 重启）
    self._temp_rect = rl.Rectangle(0, 0, 0, 0)
    self._fan_rect = rl.Rectangle(0, 0, 0, 0)
    self._mem_rect = rl.Rectangle(0, 0, 0, 0)
    # 双击检测状态（框架无原生双击事件，自行记录上次点击的卡片与时刻）
    self._last_click_action: str | None = None
    self._last_click_time = 0.0

    # 总内存只需读一次（/proc/meminfo 的 MemTotal 单位 kB）
    try:
      with open("/proc/meminfo") as f:
        for line in f:
          if line.startswith("MemTotal:"):
            self._mem_total_gb = float(line.split()[1]) / 1024.0 / 1024.0
            break
    except OSError:
      pass

    self._carrot_web_img = gui_app.texture("icons/carrot_web.png", HOME_BTN.width, HOME_BTN.height)
    self._settings_img = gui_app.texture("images/button_settings.png", SETTINGS_BTN.width, SETTINGS_BTN.height)
    self._mic_img = gui_app.texture("icons/microphone.png", 30, 30)
    self._mic_indicator_rect = rl.Rectangle(0, 0, 0, 0)
    self._font_regular = gui_app.font(FontWeight.NORMAL)
    self._font_bold = gui_app.font(FontWeight.SEMI_BOLD)

    # Callbacks
    self._on_settings_click: Callable | None = None
    self._on_carrot_web_click: Callable | None = None
    self._open_settings_callback: Callable | None = None

  def set_callbacks(self, on_settings: Callable | None = None, on_carrot_web: Callable | None = None,
                    open_settings: Callable | None = None):
    self._on_settings_click = on_settings
    self._on_carrot_web_click = on_carrot_web
    self._open_settings_callback = open_settings

  def _render(self, rect: rl.Rectangle):
    # Background
    rl.draw_rectangle_rec(rect, rl.BLACK)

    self._draw_buttons(rect)
    self._draw_network_indicator(rect)
    self._draw_metrics(rect)

  def _update_state(self):
    self._update_sidebar_metrics()

    sm = ui_state.sm
    if not sm.updated['deviceState']:
      return

    device_state = sm['deviceState']

    self._recording_audio = ui_state.recording_audio
    self._update_network_status(device_state)
    self._update_temperature_status(device_state)
    self._update_connection_status(device_state)
    self._update_panda_status()

  def _update_sidebar_metrics(self):
    """采集四张卡片所需数据（温度/CPU、内存、电压/风扇、车辆、IP）。

    与旧版 Qt sidebar.cc::updateState 同源：deviceState 给温度/内存/CPU，
    peripheralState 给电压与风扇转速，IP 从网卡枚举。
    """
    sm = ui_state.sm

    # 温度（多核平均）与 CPU 使用率（多核平均）
    try:
      temps = [float(t) for t in sm['deviceState'].cpuTempC if t > 0]
      if temps:
        self._cpu_temp = sum(temps) / len(temps)
      usages = [float(u) for u in sm['deviceState'].cpuUsagePercent if u > 0]
      if usages:
        self._cpu_usage = sum(usages) / len(usages)
      self._mem_usage = float(sm['deviceState'].memoryUsagePercent)
    except Exception:
      pass

    # 电压 / 风扇转速（peripheralState）
    try:
      ps = sm['peripheralState']
      self._voltage = float(ps.voltage) / 1000.0
      self._fan_rpm = int(ps.fanSpeedRpm)
    except Exception:
      pass

    # IP 地址：取第一个非回环、已启用的 IPv4
    self._ip_addr = "--"
    try:
      import socket
      s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
      s.connect(("8.8.8.8", 80))
      self._ip_addr = s.getsockname()[0]
      s.close()
    except Exception:
      pass

    # 车辆连接（带去抖动，详见 __init__ 处注释）
    now = time.monotonic()
    raw_online = ui_state.panda_type != log.PandaState.PandaType.unknown
    if raw_online:
      self._panda_online = True
      # 首帧（_panda_last_seen==0）立即置位，避免冷启动先闪一次 NO PANDA
      if self._panda_last_seen == 0.0:
        self._panda_last_seen = now
        self._panda_display_online = True
        self._panda_off_since = 0.0
      else:
        self._panda_last_seen = now
        if not self._panda_display_online and now - self._panda_off_since >= PANDA_ON_CONFIRM:
          self._panda_display_online = True
    else:
      # 断流：不足宽限期就保持上一次显示，避免黑屏唤醒时闪 NO PANDA
      if now - self._panda_last_seen >= PANDA_OFF_GRACE:
        self._panda_online = False
        if self._panda_display_online:
          self._panda_display_online = False
          self._panda_off_since = now

    # 风扇全速标志（CarrotFanFullSpeed，由点击温度/风扇卡切换；
    # 被 system/hardware/fan_controller.py 读取，全速时直接输出 100%）
    try:
      self._fan_full = ui_state.params.get_bool("CarrotFanFullSpeed")
    except Exception:
      self._fan_full = False

  def _update_network_status(self, device_state):
    self._net_type = NETWORK_TYPES.get(device_state.networkType.raw, tr_noop("Unknown"))
    strength = device_state.networkStrength
    self._net_strength = max(0, min(5, strength.raw + 1)) if strength.raw > 0 else 0

  def _update_temperature_status(self, device_state):
    thermal_status = device_state.thermalStatus

    if thermal_status == ThermalStatus.green:
      self._temp_status.update(tr_noop("TEMP"), tr_noop("GOOD"), Colors.GOOD)
    elif thermal_status == ThermalStatus.yellow:
      self._temp_status.update(tr_noop("TEMP"), tr_noop("OK"), Colors.WARNING)
    else:
      self._temp_status.update(tr_noop("TEMP"), tr_noop("HIGH"), Colors.DANGER)

  def _update_connection_status(self, device_state):
    last_ping = device_state.lastAthenaPingTime
    if last_ping == 0:
      self._connect_status.update(tr_noop("CONNECT"), tr_noop("OFFLINE"), Colors.WARNING)
    elif time.monotonic_ns() - last_ping < 80_000_000_000:  # 80 seconds in nanoseconds
      self._connect_status.update(tr_noop("CONNECT"), tr_noop("ONLINE"), Colors.GOOD)
    else:
      self._connect_status.update(tr_noop("CONNECT"), tr_noop("ERROR"), Colors.DANGER)

  def _update_panda_status(self):
    if ui_state.panda_type == log.PandaState.PandaType.unknown:
      self._panda_status.update(tr_noop("NO"), tr_noop("PANDA"), Colors.DANGER)
    else:
      self._panda_status.update(tr_noop("VEHICLE"), tr_noop("ONLINE"), Colors.GOOD)

  def _handle_mouse_release(self, mouse_pos: MousePos):
    # ---- 双击检测（必须放在按钮判定之前）----
    # Widget 基类只提供 _handle_mouse_press/_handle_mouse_release，没有原生双击
    # 事件（老版 Qt 有 mouseDoubleClickEvent），因此这里自行判定：同一张卡片在
    # DOUBLE_CLICK_INTERVAL 内被再次点击即视为双击。
    #   · 温度卡 / 风扇卡：切换风扇「自动 <-> 全速」
    #   · 内存卡：弹出确认重启对话框
    # ⚠️ 顺序很重要：若放在 HOME_BTN 判定之后，风扇卡与底部工具箱按钮的
    #   重叠区会被按钮先吃掉，导致「双击风扇卡无反应」。
    for rect, action in ((self._temp_rect, "fan"), (self._fan_rect, "fan"),
                         (self._mem_rect, "reboot")):
      if not rl.check_collision_point_rec(mouse_pos, rect):
        continue
      now = time.monotonic()
      if (self._last_click_action == action and
          now - self._last_click_time < DOUBLE_CLICK_INTERVAL):
        self._last_click_action = None      # 消耗掉，避免三击连发
        self._last_click_time = 0.0
        if action == "fan":
          self._toggle_fan_full_speed()
        else:
          self._confirm_reboot()
      else:
        self._last_click_action = action
        self._last_click_time = now
      return

    if rl.check_collision_point_rec(mouse_pos, SETTINGS_BTN):
      if self._on_settings_click:
        self._on_settings_click()
    elif rl.check_collision_point_rec(mouse_pos, HOME_BTN):
      if self._on_carrot_web_click:
        self._on_carrot_web_click()
    elif self._recording_audio and rl.check_collision_point_rec(mouse_pos, self._mic_indicator_rect):
      if self._open_settings_callback:
        self._open_settings_callback()

  def _toggle_fan_full_speed(self):
    """切换风扇自动/全速，并写入参数供 hardwared 的 FanController 消费。"""
    new_state = not self._fan_full
    try:
      ui_state.params.put_bool("CarrotFanFullSpeed", new_state)
      self._fan_full = ui_state.params.get_bool("CarrotFanFullSpeed")
    except Exception as e:
      # 参数写失败时回滚显示，避免卡片显示与实际风扇不一致
      print(f"[sidebar] CarrotFanFullSpeed 写入失败: {e}")
      self._fan_full = not new_state

  def _confirm_reboot(self):
    """内存卡双击：弹出确认重启对话框（与旧版 ConfirmationDialog::confirm 同语义）。"""
    from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog

    def on_confirm(result):
      if result == DialogResult.CONFIRM:
        # 等价旧版 Hardware::reboot()：延迟一点让对话框先收起
        subprocess.Popen(["sh", "-c", "sleep 0.6; sudo -n reboot"])

    dialog = ConfirmDialog("确定要重启设备吗？", "重启", callback=on_confirm)
    gui_app.push_widget(dialog)

  def _draw_buttons(self, rect: rl.Rectangle):
    mouse_pos = rl.get_mouse_position()
    mouse_down = self.is_pressed and rl.is_mouse_button_down(rl.MouseButton.MOUSE_BUTTON_LEFT)

    # Settings button
    settings_down = mouse_down and rl.check_collision_point_rec(mouse_pos, SETTINGS_BTN)
    tint = Colors.BUTTON_PRESSED if settings_down else Colors.BUTTON_NORMAL
    rl.draw_texture_ex(self._settings_img, rl.Vector2(SETTINGS_BTN.x, SETTINGS_BTN.y), 0.0, 1.0, tint)

    # Carrot Web button
    carrot_web_pressed = mouse_down and rl.check_collision_point_rec(mouse_pos, HOME_BTN)
    tint = Colors.BUTTON_PRESSED if carrot_web_pressed else Colors.BUTTON_NORMAL
    rl.draw_texture_ex(self._carrot_web_img, rl.Vector2(HOME_BTN.x, HOME_BTN.y), 0.0, 1.0, tint)

    # Microphone button
    if self._recording_audio:
      self._mic_indicator_rect = rl.Rectangle(rect.x + rect.width - 130, rect.y + 245, 75, 40)

      mic_pressed = mouse_down and rl.check_collision_point_rec(mouse_pos, self._mic_indicator_rect)
      bg_color = rl.Color(Colors.DANGER.r, Colors.DANGER.g, Colors.DANGER.b, int(255 * 0.65)) if mic_pressed else Colors.DANGER

      rl.draw_rectangle_rounded(self._mic_indicator_rect, 1, 10, bg_color)
      rl.draw_texture_ex(self._mic_img, rl.Vector2(self._mic_indicator_rect.x + (self._mic_indicator_rect.width - self._mic_img.width) / 2,
                         self._mic_indicator_rect.y + (self._mic_indicator_rect.height - self._mic_img.height) / 2), 0.0, 1.0, Colors.WHITE)

  def _draw_network_indicator(self, rect: rl.Rectangle):
    # Signal strength dots
    x_start = rect.x + 58
    y_pos = rect.y + 196
    dot_size = 27
    dot_spacing = 37

    for i in range(5):
      color = Colors.WHITE if i < self._net_strength else Colors.GRAY
      x = int(x_start + i * dot_spacing + dot_size // 2)
      y = int(y_pos + dot_size // 2)
      rl.draw_circle(x, y, dot_size // 2, color)

    # Network type text
    text_y = rect.y + 247
    text_pos = rl.Vector2(rect.x + 58, text_y)
    rl.draw_text_ex(self._font_regular, tr(self._net_type), text_pos, FONT_SIZE, 0, Colors.WHITE)

    # IP 地址（居中加粗，位于网络类型下方、卡片区上方）
    ip_size = measure_text_cached(self._font_bold, self._ip_addr, 32)
    rl.draw_text_ex(self._font_bold, self._ip_addr,
                    rl.Vector2(rect.x + (rect.width - ip_size.x) / 2, rect.y + 290),
                    32, 0, Colors.WHITE)

  def _draw_metrics(self, rect: rl.Rectangle):
    """四张系统标签卡片（移植自旧版 Qt sidebar.cc：drawMetric 圆角卡 + 阈值配色）。

    自 IP 文字底部之下、二维码区域之上，四卡等分撑满。
    """
    # 卡片区几何。⚠️ 下边界必须高于底部 HOME_BTN（工具箱按钮 y=860..1040），
    #   否则风扇卡会与按钮重叠，且 _handle_mouse_release 里 HOME_BTN 判定在前，
    #   风扇卡的下半部分点击会被按钮吃掉 —— 表现为「双击风扇卡没反应」。
    #   取 872 - 30 = 842 之下留 30px 安全边。
    card_x = rect.x + 12
    card_w = rect.width - 24
    card_top = rect.y + 350
    card_bottom = min(rect.y + rect.height - 238, rect.y + 842)   # 避开底部工具箱按钮
    card_gap = 14
    card_h = max(60, int((card_bottom - card_top - card_gap * 3) / 4))
    step = card_h + card_gap

    # 阈值配色：温度 >=85 红 / >=78 黄；内存 >=85 红 / >=70 黄；否则白
    temp_border, temp_bg, temp_w = self._threshold_color(self._cpu_temp, 85.0, 78.0)
    mem_border, mem_bg, mem_w = self._threshold_color(self._mem_usage, 85.0, 70.0)

    # 卡1：温度 + CPU
    self._temp_rect = rl.Rectangle(card_x, card_top, card_w, card_h)
    self._draw_metric(self._temp_rect, f"温度 {self._cpu_temp:.0f}°C", f"CPU {self._cpu_usage:.0f}%",
                      temp_border, temp_bg, temp_w, False, False)

    # 卡2：车辆连接
    self._panda_rect = rl.Rectangle(card_x, card_top + step, card_w, card_h)
    panda_l2 = "在线" if self._panda_display_online else "NO PANDA"
    self._draw_metric(self._panda_rect, "车辆连接", panda_l2,
                      Colors.METRIC_BORDER, rl.Color(255, 255, 255, 26), 2.0, False, False)

    # 卡3：内存（用量% + 已用/总量）
    self._mem_rect = rl.Rectangle(card_x, card_top + step * 2, card_w, card_h)
    used_gb = self._mem_usage / 100.0 * self._mem_total_gb
    self._draw_metric(self._mem_rect, f"内存 {self._mem_usage:.0f}%",
                      f"{used_gb:.1f}G / {self._mem_total_gb:.1f}G",
                      mem_border, mem_bg, mem_w, False, False)

    # 卡4：风扇 + 电压（全速态第二行显示转速并用绿色）
    self._fan_rect = rl.Rectangle(card_x, card_top + step * 3, card_w, card_h)
    if self._fan_full:
      fan_l1, fan_l2 = "风扇 全速", f"{self._fan_rpm}"
    else:
      fan_l1, fan_l2 = "风扇 自动", f"电压 {self._voltage:.2f}V"
    self._draw_metric(self._fan_rect, fan_l1, fan_l2,
                      temp_border, temp_bg, temp_w, self._fan_full, self._fan_full)

  @staticmethod
  def _threshold_color(value: float, danger_at: float, warn_at: float):
    """阈值配色：超红 → 红底，超黄 → 黄底，否则白底。返回 (边框色, 底色, 边框宽)。"""
    if value >= danger_at:
      return rl.Color(201, 34, 49, 255), rl.Color(255, 77, 77, 31), 3.0
    if value >= warn_at:
      return rl.Color(218, 202, 37, 255), rl.Color(255, 194, 51, 31), 3.0
    return rl.Color(255, 255, 255, 85), rl.Color(255, 255, 255, 26), 2.0

  def _draw_metric(self, metric_rect: rl.Rectangle, l1: str, l2: str,
                   border: rl.Color, bg: rl.Color, border_w: float,
                   second_green: bool, second_bold: bool):
    """单张卡片：圆角底色 + 圆角边框 + 居中两行加粗文字。"""
    rl.draw_rectangle_rounded(metric_rect, 0.15, 18, bg)
    rl.draw_rectangle_rounded_lines_ex(metric_rect, 0.15, 18, border_w, border)

    fs1 = 34
    fs2 = 40 if second_bold else 34
    line_h = 46
    line_gap = 4
    top_pad = max(6, (metric_rect.height - line_h * 2 - line_gap) // 2)

    self._draw_centered_line(metric_rect, l1, top_pad, line_h, fs1, Colors.WHITE)
    l2_color = rl.Color(0, 255, 0, 255) if second_green else Colors.WHITE
    self._draw_centered_line(metric_rect, l2, top_pad + line_h + line_gap, line_h, fs2, l2_color)

  def _draw_centered_line(self, rect: rl.Rectangle, text: str, y_offset: int, line_h: int,
                          font_size: int, color: rl.Color):
    size = measure_text_cached(self._font_bold, text, font_size)
    x = rect.x + (rect.width - size.x) / 2
    y = rect.y + y_offset + (line_h - size.y) / 2
    rl.draw_text_ex(self._font_bold, text, rl.Vector2(x, y), font_size, 0, color)
