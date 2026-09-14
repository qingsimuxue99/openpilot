import pyray as rl
import time
from dataclasses import dataclass
from collections.abc import Callable
from openpilot.cereal import log
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos, FONT_SCALE
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

from openpilot.selfdrive.ui.sunnypilot.layouts.sidebar import SidebarSP

SIDEBAR_WIDTH = 300
METRIC_HEIGHT = 126
METRIC_WIDTH = 240
METRIC_MARGIN = 30
FONT_SIZE = 38

SETTINGS_BTN = rl.Rectangle(50, 35, 200, 117)
HOME_BTN = rl.Rectangle(60, 860, 180, 180)

ThermalStatus = log.DeviceState.ThermalStatus
NetworkType = log.DeviceState.NetworkType


class Colors:
  WHITE = rl.WHITE
  WHITE_DIM = rl.Color(255, 255, 255, 85)
  GRAY = rl.Color(84, 84, 84, 255)
  GOOD = rl.WHITE
  WARNING = rl.Color(218, 202, 37, 255)
  DANGER = rl.Color(201, 34, 49, 255)
  METRIC_BG = rl.Color(45, 45, 48, 255)
  METRIC_BORDER = rl.Color(255, 255, 255, 60)


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
  line1: str
  line2: str
  color: rl.Color = Colors.WHITE

  def update(self, line1: str, line2: str, color: rl.Color = Colors.WHITE):
    self.line1 = line1
    self.line2 = line2
    self.color = color


class Sidebar(Widget, SidebarSP):
  def __init__(self):
    Widget.__init__(self)
    SidebarSP.__init__(self)
    self._net_type = NETWORK_TYPES[NetworkType.none]
    self._net_strength = 0
    self._ip_address = "--"

    # 四个指标卡片
    self._metric_temp = MetricData("温度 --°C", "CPU --%")
    self._metric_vehicle = MetricData("车辆连接", "离线")
    self._metric_memory = MetricData("内存 --%", "--G / --G")
    self._metric_fan = MetricData("风扇 自动", "电压 --.--V")

    self._home_img = gui_app.texture("images/button_home.png", HOME_BTN.width, HOME_BTN.height)
    self._flag_img = gui_app.texture("images/button_flag.png", HOME_BTN.width, HOME_BTN.height)
    self._settings_img = gui_app.texture("images/button_settings.png", SETTINGS_BTN.width, SETTINGS_BTN.height)
    self._mic_img = gui_app.texture("icons/microphone.png", 30, 30)
    self._mic_indicator_rect = rl.Rectangle(0, 0, 0, 0)
    self._font_regular = gui_app.font(FontWeight.NORMAL)
    self._font_bold = gui_app.font(FontWeight.SEMI_BOLD)

    self._on_settings_click: Callable | None = None
    self._on_flag_click: Callable | None = None
    self._open_settings_callback: Callable | None = None

  def set_callbacks(self, on_settings: Callable | None = None, on_flag: Callable | None = None,
                    open_settings: Callable | None = None):
    self._on_settings_click = on_settings
    self._on_flag_click = on_flag
    self._open_settings_callback = open_settings

  def _render(self, rect: rl.Rectangle):
    rl.draw_rectangle_rec(rect, rl.BLACK)
    self._draw_buttons(rect)
    self._draw_network_indicator(rect)
    self._draw_metrics(rect)

  def _update_state(self):
    sm = ui_state.sm
    if not sm.updated['deviceState']:
      return
    device_state = sm['deviceState']
    self._recording_audio = ui_state.recording_audio
    self._update_network_status(device_state)
    self._update_detailed_metrics(device_state)
    SidebarSP._update_sunnylink_status(self)

  def _update_network_status(self, device_state):
    self._net_type = NETWORK_TYPES.get(device_state.networkType.raw, tr_noop("Unknown"))
    strength = device_state.networkStrength
    self._net_strength = max(0, min(5, strength.raw + 1)) if strength.raw > 0 else 0
    self._ip_address = device_state.ipAddress or "--"

  def _update_detailed_metrics(self, device_state):
    # 温度 + CPU
    try:
        cpu_temp = max(device_state.cpuTempC) if device_state.cpuTempC else 0
    except Exception:
        cpu_temp = 0
    try:
        cpu_usage = device_state.cpuUsagePercent
    except Exception:
        cpu_usage = 0
    self._metric_temp.update(f"温度 {int(cpu_temp)}°C", f"CPU {int(cpu_usage)}%")

    # 车辆连接
    if ui_state.panda_type == log.PandaState.PandaType.unknown:
        self._metric_vehicle.update("车辆连接", "未连接", Colors.DANGER)
    else:
        self._metric_vehicle.update("车辆连接", "在线", Colors.WHITE)

    # 内存
    try:
        mem_pct = device_state.memoryUsagePercent
        mem_total = device_state.memoryUsageTotal
        mem_used = mem_total * mem_pct / 100
        self._metric_memory.update(f"内存 {int(mem_pct)}%", f"{mem_used:.1f}G / {mem_total:.1f}G")
    except Exception:
        self._metric_memory.update("内存 --%", "--G / --G")

    # 风扇 + 电压
    try:
        fan_speed = device_state.fanSpeedPercent
        fan_text = "风扇 自动" if fan_speed < 10 else f"风扇 {int(fan_speed)}%"
    except Exception:
        fan_text = "风扇 自动"
    try:
        voltage = device_state.batteryVoltageMilliVolt / 1000.0
    except Exception:
        voltage = 0
    self._metric_fan.update(fan_text, f"电压 {voltage:.2f}V")

  def _handle_mouse_release(self, mouse_pos: MousePos):
    if rl.check_collision_point_rec(mouse_pos, SETTINGS_BTN):
      if self._on_settings_click:
        self._on_settings_click()
    elif rl.check_collision_point_rec(mouse_pos, HOME_BTN) and ui_state.started:
      if self._on_flag_click:
        self._on_flag_click()
    elif self._recording_audio and rl.check_collision_point_rec(mouse_pos, self._mic_indicator_rect):
      if self._open_settings_callback:
        self._open_settings_callback()

  def _draw_buttons(self, rect: rl.Rectangle):
    mouse_pos = rl.get_mouse_position()
    mouse_down = self.is_pressed and rl.is_mouse_button_down(rl.MouseButton.MOUSE_BUTTON_LEFT)

    settings_down = mouse_down and rl.check_collision_point_rec(mouse_pos, SETTINGS_BTN)
    tint = rl.Color(255, 255, 255, 166) if settings_down else Colors.WHITE
    rl.draw_texture_ex(self._settings_img, rl.Vector2(SETTINGS_BTN.x, SETTINGS_BTN.y), 0.0, 1.0, tint)

    flag_pressed = mouse_down and rl.check_collision_point_rec(mouse_pos, HOME_BTN)
    button_img = self._flag_img if ui_state.started else self._home_img
    button_pos = rl.Vector2(HOME_BTN.x, HOME_BTN.y)
    icon_opacity = 1.0

    if gui_app.sunnypilot_ui():
      button_img, button_pos, icon_opacity = SidebarSP._get_home_icon(self, button_img)

    tint = rl.Color(255, 255, 255, 166) if (ui_state.started and flag_pressed) else Colors.WHITE
    if icon_opacity < 1.0:
      tint = rl.Color(tint[0], tint[1], tint[2], int(255 * icon_opacity))
    rl.draw_texture_ex(button_img, button_pos, 0.0, 1.0, tint)

    if self._recording_audio:
      self._mic_indicator_rect = rl.Rectangle(rect.x + rect.width - 130, rect.y + 245, 75, 40)
      mic_pressed = mouse_down and rl.check_collision_point_rec(mouse_pos, self._mic_indicator_rect)
      bg_color = rl.Color(Colors.DANGER.r, Colors.DANGER.g, Colors.DANGER.b, int(255 * 0.65)) if mic_pressed else Colors.DANGER
      rl.draw_rectangle_rounded(self._mic_indicator_rect, 1, 10, bg_color)
      rl.draw_texture_ex(self._mic_img, rl.Vector2(self._mic_indicator_rect.x + (self._mic_indicator_rect.width - self._mic_img.width) / 2,
                         self._mic_indicator_rect.y + (self._mic_indicator_rect.height - self._mic_img.height) / 2), 0.0, 1.0, Colors.WHITE)

  def _draw_network_indicator(self, rect: rl.Rectangle):
    # 信号圆点
    x_start = rect.x + 58
    y_pos = rect.y + 196
    dot_size = 27
    dot_spacing = 37

    for i in range(5):
      color = Colors.WHITE if i < self._net_strength else Colors.GRAY
      x = int(x_start + i * dot_spacing + dot_size // 2)
      y = int(y_pos + dot_size // 2)
      rl.draw_circle(x, y, dot_size // 2, color)

    # 网络类型
    text_y = rect.y + 247
    text_pos = rl.Vector2(rect.x + 58, text_y)
    rl.draw_text_ex(self._font_regular, tr(self._net_type), text_pos, 42, 0, Colors.WHITE)

    # IP 地址
    ip_y = rect.y + 300
    ip_pos = rl.Vector2(rect.x + 40, ip_y)
    rl.draw_text_ex(self._font_regular, self._ip_address, ip_pos, 38, 0, Colors.WHITE)

  def _draw_metrics(self, rect: rl.Rectangle):
    metrics = [self._metric_temp, self._metric_vehicle, self._metric_memory, self._metric_fan]
    start_y = int(rect.y) + 360
    spacing = 155

    for idx, metric in enumerate(metrics):
      self._draw_metric(rect, metric, start_y + idx * spacing)

  def _draw_metric(self, rect: rl.Rectangle, metric: MetricData, y: float):
    metric_rect = rl.Rectangle(rect.x + METRIC_MARGIN, y, METRIC_WIDTH, METRIC_HEIGHT)
    # 深色背景圆角矩形
    rl.draw_rectangle_rounded(metric_rect, 0.15, 12, Colors.METRIC_BG)
    rl.draw_rectangle_rounded_lines_ex(metric_rect, 0.15, 12, 2, Colors.METRIC_BORDER)

    # 两行文字居中
    lines = [metric.line1, metric.line2]
    total_text_height = len(lines) * FONT_SIZE * FONT_SCALE
    text_y = metric_rect.y + (metric_rect.height - total_text_height) / 2

    for text in lines:
      text_size = measure_text_cached(self._font_bold, text, FONT_SIZE)
      text_x = metric_rect.x + (metric_rect.width - text_size.x) / 2
      rl.draw_text_ex(self._font_bold, text, rl.Vector2(int(text_x), int(text_y)), FONT_SIZE, 0, metric.color)
      text_y += text_size.y
