import pyray as rl
from dataclasses import dataclass
from openpilot.common.constants import CV
from openpilot.selfdrive.ui.onroad.exp_button import ExpButton
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget

# Constants
SET_SPEED_NA = 255
KM_TO_MILE = 0.621371
CRUISE_DISABLED_CHAR = '–'


@dataclass(frozen=True)
class UIConfig:
  header_height: int = 300
  border_size: int = 30
  button_size: int = 192
  set_speed_width_metric: int = 200
  set_speed_width_imperial: int = 172
  set_speed_height: int = 204
  wheel_icon_size: int = 144


@dataclass(frozen=True)
class FontSizes:
  current_speed: int = 176
  speed_unit: int = 66
  max_speed: int = 40
  set_speed: int = 90


@dataclass(frozen=True)
class Colors:
  WHITE = rl.WHITE
  DISENGAGED = rl.Color(145, 155, 149, 255)
  OVERRIDE = rl.Color(145, 155, 149, 255)  # Added
  ENGAGED = rl.Color(128, 216, 166, 255)
  DISENGAGED_BG = rl.Color(0, 0, 0, 153)
  OVERRIDE_BG = rl.Color(145, 155, 149, 204)
  ENGAGED_BG = rl.Color(128, 216, 166, 204)
  GREY = rl.Color(166, 166, 166, 255)
  DARK_GREY = rl.Color(114, 114, 114, 255)
  BLACK_TRANSLUCENT = rl.Color(0, 0, 0, 166)
  WHITE_TRANSLUCENT = rl.Color(255, 255, 255, 200)
  BORDER_TRANSLUCENT = rl.Color(255, 255, 255, 75)
  HEADER_GRADIENT_START = rl.Color(0, 0, 0, 114)
  HEADER_GRADIENT_END = rl.BLANK


UI_CONFIG = UIConfig()
FONT_SIZES = FontSizes()
COLORS = Colors()


class HudRenderer(Widget):
  def __init__(self):
    super().__init__()
    """Initialize the HUD renderer."""
    self.is_cruise_set: bool = False
    self.is_cruise_available: bool = True
    self.set_speed: float = SET_SPEED_NA
    self.speed: float = 0.0
    self.v_ego_cluster_seen: bool = False

    self._font_semi_bold: rl.Font = gui_app.font(FontWeight.SEMI_BOLD)
    self._font_bold: rl.Font = gui_app.font(FontWeight.BOLD)
    self._font_medium: rl.Font = gui_app.font(FontWeight.MEDIUM)

    self._exp_button: ExpButton = ExpButton(UI_CONFIG.button_size, UI_CONFIG.wheel_icon_size)

    # CP 移植：左下角状态胶囊（弯道限速 + 红灯停等）
    self._capsule_counter = 0
    self._capsule_stop_state = 0

  def _update_state(self) -> None:
    """Update HUD state based on car state and controls state."""
    sm = ui_state.sm
    if sm.recv_frame["carState"] < ui_state.started_frame:
      self.is_cruise_set = False
      self.set_speed = SET_SPEED_NA
      self.speed = 0.0
      return

    controls_state = sm['controlsState']
    car_state = sm['carState']

    v_cruise_cluster = car_state.vCruiseCluster
    self.set_speed = (
      controls_state.deprecated.vCruise if v_cruise_cluster == 0.0 else v_cruise_cluster
    )
    self.is_cruise_set = 0 < self.set_speed < SET_SPEED_NA
    self.is_cruise_available = self.set_speed != -1

    if self.is_cruise_set and not ui_state.is_metric:
      self.set_speed *= KM_TO_MILE

    v_ego_cluster = car_state.vEgoCluster
    self.v_ego_cluster_seen = self.v_ego_cluster_seen or v_ego_cluster != 0.0
    v_ego = v_ego_cluster if self.v_ego_cluster_seen else car_state.vEgo
    speed_conversion = CV.MS_TO_KPH if ui_state.is_metric else CV.MS_TO_MPH
    self.speed = max(0.0, v_ego * speed_conversion)

  def _render(self, rect: rl.Rectangle) -> None:
    """Render HUD elements to the screen."""
    # Draw the header background
    rl.draw_rectangle_gradient_v(
      int(rect.x),
      int(rect.y),
      int(rect.width),
      UI_CONFIG.header_height,
      COLORS.HEADER_GRADIENT_START,
      COLORS.HEADER_GRADIENT_END,
    )

    if self.is_cruise_available:
      self._draw_set_speed(rect)

    self._draw_current_speed(rect)

    button_x = rect.x + rect.width - UI_CONFIG.border_size - UI_CONFIG.button_size
    button_y = rect.y + UI_CONFIG.border_size
    self._exp_button.render(rl.Rectangle(button_x, button_y, UI_CONFIG.button_size, UI_CONFIG.button_size))

    # 左下角胶囊图标：弯道限速 + 红灯减速
    self._draw_status_capsules(rect)

  def user_interacting(self) -> bool:
    return self._exp_button.is_pressed

  def _draw_set_speed(self, rect: rl.Rectangle) -> None:
    """Draw the MAX speed indicator box."""
    set_speed_width = UI_CONFIG.set_speed_width_metric if ui_state.is_metric else UI_CONFIG.set_speed_width_imperial
    x = rect.x + 60 + (UI_CONFIG.set_speed_width_imperial - set_speed_width) // 2
    y = rect.y + 45

    set_speed_rect = rl.Rectangle(x, y, set_speed_width, UI_CONFIG.set_speed_height)
    rl.draw_rectangle_rounded(set_speed_rect, 0.35, 10, COLORS.BLACK_TRANSLUCENT)
    rl.draw_rectangle_rounded_lines_ex(set_speed_rect, 0.35, 10, 6, COLORS.BORDER_TRANSLUCENT)

    max_color = COLORS.GREY
    set_speed_color = COLORS.DARK_GREY
    if self.is_cruise_set:
      set_speed_color = COLORS.WHITE
      if ui_state.status == UIStatus.ENGAGED:
        max_color = COLORS.ENGAGED
      elif ui_state.status == UIStatus.DISENGAGED:
        max_color = COLORS.DISENGAGED
      elif ui_state.status == UIStatus.OVERRIDE:
        max_color = COLORS.OVERRIDE

    max_text = tr("MAX")
    max_text_width = measure_text_cached(self._font_semi_bold, max_text, FONT_SIZES.max_speed).x
    rl.draw_text_ex(
      self._font_semi_bold,
      max_text,
      rl.Vector2(x + (set_speed_width - max_text_width) / 2, y + 27),
      FONT_SIZES.max_speed,
      0,
      max_color,
    )

    set_speed_text = CRUISE_DISABLED_CHAR if not self.is_cruise_set else str(round(self.set_speed))
    speed_text_width = measure_text_cached(self._font_bold, set_speed_text, FONT_SIZES.set_speed).x
    rl.draw_text_ex(
      self._font_bold,
      set_speed_text,
      rl.Vector2(x + (set_speed_width - speed_text_width) / 2, y + 77),
      FONT_SIZES.set_speed,
      0,
      set_speed_color,
    )

  def _draw_current_speed(self, rect: rl.Rectangle) -> None:
    """Draw the current vehicle speed and unit."""
    speed_text = str(round(self.speed))
    speed_text_size = measure_text_cached(self._font_bold, speed_text, FONT_SIZES.current_speed)
    speed_pos = rl.Vector2(rect.x + rect.width / 2 - speed_text_size.x / 2, 180 - speed_text_size.y / 2)
    rl.draw_text_ex(self._font_bold, speed_text, speed_pos, FONT_SIZES.current_speed, 0, COLORS.WHITE)

    unit_text = tr("km/h") if ui_state.is_metric else tr("mph")
    unit_text_size = measure_text_cached(self._font_medium, unit_text, FONT_SIZES.speed_unit)
    unit_pos = rl.Vector2(rect.x + rect.width / 2 - unit_text_size.x / 2, 290 - unit_text_size.y / 2)
    rl.draw_text_ex(self._font_medium, unit_text, unit_pos, FONT_SIZES.speed_unit, 0, COLORS.WHITE_TRANSLUCENT)


  # ---- 左下角胶囊布局常量 ----
  # 底部「状态栏」= SP 的前车距离/速度/TTC 显示（chevron_metrics），
  # 它逐行往上排，最底一行距内容区底边 _LEAD_MARGIN。
  _CAPSULE_EDGE = 20        # 胶囊距左边框
  _CAPSULE_GAP = 20         # 胶囊距下方状态栏（前车距离）上沿
  _CAPSULE_STACK_GAP = 10   # 两个胶囊同时出现时的间距
  _CAPSULE_H = 30
  _LEAD_MARGIN = 20         # 与 chevron_metrics.margin 保持一致
  _LEAD_LINE_H = 50         # 与 chevron_metrics.line_height 保持一致

  # 胶囊配色：未激活=中性灰（常驻可见，用来确认功能在线）；激活后转成功能色
  _CAPSULE_COLOR_IDLE = rl.Color(110, 110, 110, 190)         # 未激活：灰
  _CAPSULE_COLOR_ACTIVE_CURVE = rl.Color(34, 139, 34, 220)   # 弯道限速激活：绿
  _CAPSULE_COLOR_ACTIVE_STOP = rl.Color(220, 50, 50, 220)    # 红绿灯/停止激活：红

  def _lead_status_line_count(self) -> int:
    """底部状态栏当前占几行（0 = 未开启）。ChevronInfo: 1=距离 2=速度 3=TTC 4=全部。"""
    try:
      opt = int(getattr(ui_state, "chevron_metrics", 0) or 0)
    except Exception:
      opt = 0
    lines = 0
    if opt in (1, 4):
      lines += 1
    if opt in (2, 4):
      lines += 1
    if opt in (3, 4):
      lines += 1
    return lines

  def _draw_status_capsules(self, rect: rl.Rectangle) -> None:
    """左下角状态胶囊：弯道限速（激活=绿）/ 红绿灯停等（激活=红）。

    两个胶囊**常驻显示**：未激活时为灰色，激活后转成对应的功能色，
    这样一眼就能确认功能是否在线、当前是否在工作。
    位置：距左边框 20px、距底部状态栏（前车距离/速度/TTC）上沿 20px；
    两个胶囊固定上下堆叠（弯道限速在下、红绿灯在上），位置不随状态跳变。
    """
    sm = ui_state.sm
    h = self._CAPSULE_H
    x = rect.x + self._CAPSULE_EDGE

    lines = self._lead_status_line_count()
    status_block = (self._LEAD_MARGIN + lines * self._LEAD_LINE_H) if lines else 0
    base_y = rect.y + rect.height - status_block - self._CAPSULE_GAP - h

    # 1. 弯道限速胶囊（未激活灰 / 激活绿）
    curve_active = False
    try:
      if sm.recv_frame.get("longitudinalPlanSP", 0) >= ui_state.started_frame:
        curve_active = bool(sm["longitudinalPlanSP"].smartCruiseControl.vision.active)
    except Exception:
      curve_active = False
    self._draw_capsule(x, base_y, h, "Curve Limit",
                       self._CAPSULE_COLOR_ACTIVE_CURVE if curve_active else self._CAPSULE_COLOR_IDLE)

    # 2. 红灯/停止标志停等胶囊（未激活灰 / 激活红）。每秒读一次参数，够用且不占资源。
    try:
      if self._capsule_counter % 20 == 0:
        v = ui_state.params.get("TrafficStopState", return_default=True)
        self._capsule_stop_state = int(v) if v is not None else 0
      self._capsule_counter += 1
    except Exception:
      pass

    if self._capsule_stop_state in (1, 2):
      text = "Red Light" if self._capsule_stop_state == 1 else "Stopped"
      color = self._CAPSULE_COLOR_ACTIVE_STOP
    else:
      text = "Red Light"
      color = self._CAPSULE_COLOR_IDLE
    self._draw_capsule(x, base_y - (h + self._CAPSULE_STACK_GAP), h, text, color)

  def _draw_capsule(self, x: float, y: float, height: float, text: str, color: rl.Color) -> None:
    """CP 移植：画一个左下角状态胶囊（全圆角 pill）。

    ⚠️ comma 的 raylib 分支 ABI（实测反射 _raylib_cffi_comma.lib）：
      • `TextLength(char*)` —— **只吃 text，没有 font 参数**，不能用来按字体量宽；
        要带字体量宽必须用 `measure_text_cached(font, text, size)`。
      • `DrawRectangleRounded(Rectangle, float roundness, int segments, Color)`
        —— 第一个参数是 **Rectangle 对象**，不是 x/y/w/h 四个数。
      roundness=1.0 → 圆角半径 = 短边一半 = 完整胶囊。
    """
    font_size = 20
    spacing = 1
    text_width = measure_text_cached(self._font_medium, text, font_size, spacing).x
    width = text_width + 30
    rl.draw_rectangle_rounded(rl.Rectangle(float(x), float(y), float(width), float(height)), 1.0, 10, color)
    rl.draw_text_ex(self._font_medium, text, rl.Vector2(x + 15, y + 6), font_size, spacing, rl.WHITE)
