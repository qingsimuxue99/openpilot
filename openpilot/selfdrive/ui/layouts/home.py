import io
import socket
import time
import pyray as rl
from collections.abc import Callable
from enum import IntEnum
from openpilot.common.params import Params
from openpilot.selfdrive.ui.widgets.offroad_alerts import UpdateAlert, OffroadAlert
from openpilot.selfdrive.ui.widgets.exp_mode_button import ExperimentalModeButton
from openpilot.selfdrive.ui.widgets.prime import PrimeWidget
from openpilot.selfdrive.ui.widgets.setup import SetupWidget
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos, TextAlignment
from openpilot.system.ui.lib.multilang import tr, trn
from openpilot.system.ui.widgets.label import gui_label
from openpilot.system.ui.widgets import Widget

HEADER_HEIGHT = 80
HEAD_BUTTON_FONT_SIZE = 40
CONTENT_MARGIN = 40
SPACING = 25
RIGHT_COLUMN_WIDTH = 750
REFRESH_INTERVAL = 10.0


class HomeLayoutState(IntEnum):
  HOME = 0
  UPDATE = 1
  ALERTS = 2


class HomeLayout(Widget):
  def __init__(self):
    super().__init__()
    self.params = Params()

    self.update_alert = UpdateAlert()
    self.offroad_alert = OffroadAlert()

    self._layout_widgets = {HomeLayoutState.UPDATE: self.update_alert, HomeLayoutState.ALERTS: self.offroad_alert}

    self.current_state = HomeLayoutState.HOME
    self.last_refresh = 0
    self.settings_callback: Callable[[], None] | None = None

    self.update_available = False
    self.alert_count = 0
    self._version_text = ""
    self._prev_update_available = False
    self._prev_alerts_present = False

    self.header_rect = rl.Rectangle(0, 0, 0, 0)
    self.content_rect = rl.Rectangle(0, 0, 0, 0)
    self.left_column_rect = rl.Rectangle(0, 0, 0, 0)
    self.right_column_rect = rl.Rectangle(0, 0, 0, 0)

    self.update_notif_rect = rl.Rectangle(0, 0, 200, HEADER_HEIGHT - 10)
    self.alert_notif_rect = rl.Rectangle(0, 0, 220, HEADER_HEIGHT - 10)

    self._prime_widget = PrimeWidget()
    self._setup_widget = SetupWidget()

    self._exp_mode_button = ExperimentalModeButton()
    self._setup_callbacks()

    # 网页工具箱二维码：纹理按 URL 缓存，IP 变化时自动重建
    self._qr_texture = None
    self._qr_url = ""
    self._qr_ip: str | None = None
    self._qr_ip_time = 0.0

  def show_event(self):
    super().show_event()
    self._exp_mode_button.show_event()
    self.last_refresh = time.monotonic()
    self._refresh()

  def _setup_callbacks(self):
    self.update_alert.set_dismiss_callback(lambda: self._set_state(HomeLayoutState.HOME))
    self.offroad_alert.set_dismiss_callback(lambda: self._set_state(HomeLayoutState.HOME))
    self._exp_mode_button.set_click_callback(lambda: self.settings_callback() if self.settings_callback else None)

  def set_settings_callback(self, callback: Callable):
    self.settings_callback = callback

  def _set_state(self, state: HomeLayoutState):
    # propagate show/hide events
    if state != self.current_state:
      if state == HomeLayoutState.HOME:
        self._exp_mode_button.show_event()

      if state in self._layout_widgets:
        self._layout_widgets[state].show_event()
      if self.current_state in self._layout_widgets:
        self._layout_widgets[self.current_state].hide_event()

    self.current_state = state

  def _render(self, rect: rl.Rectangle):
    current_time = time.monotonic()
    if current_time - self.last_refresh >= REFRESH_INTERVAL:
      self._refresh()
      self.last_refresh = current_time

    self._render_header()

    # Render content based on current state
    if self.current_state == HomeLayoutState.HOME:
      self._render_home_content()
    elif self.current_state == HomeLayoutState.UPDATE:
      self._render_update_view()
    elif self.current_state == HomeLayoutState.ALERTS:
      self._render_alerts_view()

  def _update_state(self):
    self.header_rect = rl.Rectangle(
      self._rect.x + CONTENT_MARGIN, self._rect.y + CONTENT_MARGIN, self._rect.width - 2 * CONTENT_MARGIN, HEADER_HEIGHT
    )

    content_y = self._rect.y + CONTENT_MARGIN + HEADER_HEIGHT + SPACING
    content_height = self._rect.height - CONTENT_MARGIN - HEADER_HEIGHT - SPACING - CONTENT_MARGIN

    self.content_rect = rl.Rectangle(
      self._rect.x + CONTENT_MARGIN, content_y, self._rect.width - 2 * CONTENT_MARGIN, content_height
    )

    left_width = self.content_rect.width - RIGHT_COLUMN_WIDTH - SPACING

    self.left_column_rect = rl.Rectangle(self.content_rect.x, self.content_rect.y, left_width, self.content_rect.height)

    self.right_column_rect = rl.Rectangle(
      self.content_rect.x + left_width + SPACING, self.content_rect.y, RIGHT_COLUMN_WIDTH, self.content_rect.height
    )

    self.update_notif_rect.x = self.header_rect.x
    self.update_notif_rect.y = self.header_rect.y + (self.header_rect.height - 60) // 2

    notif_x = self.header_rect.x + (220 if self.update_available else 0)
    self.alert_notif_rect.x = notif_x
    self.alert_notif_rect.y = self.header_rect.y + (self.header_rect.height - 60) // 2

  def _handle_mouse_release(self, mouse_pos: MousePos):
    super()._handle_mouse_release(mouse_pos)

    if self.update_available and rl.check_collision_point_rec(mouse_pos, self.update_notif_rect):
      self._set_state(HomeLayoutState.UPDATE)
    elif self.alert_count > 0 and rl.check_collision_point_rec(mouse_pos, self.alert_notif_rect):
      self._set_state(HomeLayoutState.ALERTS)

  def _render_header(self):
    font = gui_app.font(FontWeight.MEDIUM)

    version_text_width = self.header_rect.width

    # Update notification button
    if self.update_available:
      version_text_width -= self.update_notif_rect.width

      # Highlight if currently viewing updates
      highlight_color = rl.Color(75, 95, 255, 255) if self.current_state == HomeLayoutState.UPDATE else rl.Color(54, 77, 239, 255)
      rl.draw_rectangle_rounded(self.update_notif_rect, 0.3, 10, highlight_color)

      text = tr("UPDATE")
      text_size = measure_text_cached(font, text, HEAD_BUTTON_FONT_SIZE)
      text_x = self.update_notif_rect.x + (self.update_notif_rect.width - text_size.x) // 2
      text_y = self.update_notif_rect.y + (self.update_notif_rect.height - text_size.y) // 2
      rl.draw_text_ex(font, text, rl.Vector2(int(text_x), int(text_y)), HEAD_BUTTON_FONT_SIZE, 0, rl.WHITE)

    # Alert notification button
    if self.alert_count > 0:
      version_text_width -= self.alert_notif_rect.width

      # Highlight if currently viewing alerts
      highlight_color = rl.Color(255, 70, 70, 255) if self.current_state == HomeLayoutState.ALERTS else rl.Color(226, 44, 44, 255)
      rl.draw_rectangle_rounded(self.alert_notif_rect, 0.3, 10, highlight_color)

      alert_text = trn("{} ALERT", "{} ALERTS", self.alert_count).format(self.alert_count)
      text_size = measure_text_cached(font, alert_text, HEAD_BUTTON_FONT_SIZE)
      text_x = self.alert_notif_rect.x + (self.alert_notif_rect.width - text_size.x) // 2
      text_y = self.alert_notif_rect.y + (self.alert_notif_rect.height - text_size.y) // 2
      rl.draw_text_ex(font, alert_text, rl.Vector2(int(text_x), int(text_y)), HEAD_BUTTON_FONT_SIZE, 0, rl.WHITE)

    # Version text (right aligned)
    if self.update_available or self.alert_count > 0:
      version_text_width -= SPACING * 1.5

    version_rect = rl.Rectangle(self.header_rect.x + self.header_rect.width - version_text_width, self.header_rect.y,
                                version_text_width, self.header_rect.height)
    gui_label(version_rect, self._version_text, 48, rl.WHITE, alignment=TextAlignment.RIGHT)

  def _render_home_content(self):
    self._render_left_column()
    self._render_right_column()

  def _render_update_view(self):
    self.update_alert.render(self.content_rect)

  def _render_alerts_view(self):
    self.offroad_alert.render(self.content_rect)

  def _render_left_column(self):
    self._prime_widget.render(self.left_column_rect)

  def _render_right_column(self):
    exp_height = 125
    exp_rect = rl.Rectangle(
      self.right_column_rect.x, self.right_column_rect.y, self.right_column_rect.width, exp_height
    )
    self._exp_mode_button.render(exp_rect)

    # 右边画工具箱二维码
    qr_rect = rl.Rectangle(
      self.right_column_rect.x,
      self.right_column_rect.y + exp_height + SPACING,
      self.right_column_rect.width,
      self.right_column_rect.height - exp_height - SPACING,
    )
    self._render_qr_code(qr_rect)

  def _get_device_ip(self) -> str:
    """取设备当前 IPv4。3 秒内复用缓存，因此网络/IP 变化后会自动跟随。"""
    now = time.monotonic()
    if self._qr_ip is not None and now - self._qr_ip_time < 3.0:
      return self._qr_ip

    ip = ""

    # 1) 找默认路由的出口网卡，再直接读该网卡 IPv4（不发包、不依赖外网）
    try:
      import fcntl
      import struct

      iface = ""
      with open("/proc/net/route", encoding="utf-8") as f:
        next(f)
        for line in f:
          cols = line.split()
          # Destination=00000000 且 Mask=00000000 即默认路由
          if len(cols) >= 8 and cols[1] == "00000000" and cols[7] == "00000000":
            iface = cols[0]
            break

      if iface:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
          # SIOCGIFADDR = 0x8915
          packed = struct.pack("256s", iface[:15].encode("utf-8"))
          ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, packed)[20:24])
        finally:
          s.close()
    except Exception:
      ip = ""

    # 2) 兜底：UDP connect 探路由（只查路由表，不发包）
    if not ip or ip.startswith("127."):
      for target in (("8.8.8.8", 80), ("1.1.1.1", 53), ("192.168.5.1", 80)):
        try:
          s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
          try:
            s.connect(target)
            probe = s.getsockname()[0]
          finally:
            s.close()
          if probe and not probe.startswith("127."):
            ip = probe
            break
        except Exception:
          continue

    if not ip:
      ip = "127.0.0.1"

    self._qr_ip = ip
    self._qr_ip_time = now
    return ip

  def _get_qr_texture(self, url: str):
    """按 URL 缓存二维码纹理，只有 URL 变了才重新生成；失败返回 None。"""
    if url == self._qr_url and self._qr_texture is not None:
      return self._qr_texture

    if self._qr_texture is not None:
      try:
        rl.unload_texture(self._qr_texture)
      except Exception:
        pass
      self._qr_texture = None
      self._qr_url = ""

    try:
      import qrcode

      qr = qrcode.QRCode(version=1, box_size=20, border=2)
      qr.add_data(url)
      qr.make(fit=True)
      img = qr.make_image(fill_color="black", back_color="white")

      buf = io.BytesIO()
      img.save(buf, format="PNG")
      data = buf.getvalue()

      qr_img = rl.load_image_from_memory(".png", data, len(data))
      texture = rl.load_texture_from_image(qr_img)
      rl.unload_image(qr_img)

      if texture.width <= 0 or texture.height <= 0:
        raise RuntimeError(f"纹理尺寸异常 {texture.width}x{texture.height}")

      rl.set_texture_filter(texture, rl.TEXTURE_FILTER_BILINEAR)
      self._qr_texture = texture
      self._qr_url = url
    except Exception as e:
      print(f"[home] 二维码生成失败: {type(e).__name__}: {e}")
      self._qr_texture = None
      self._qr_url = ""

    return self._qr_texture

  def _render_qr_code(self, rect: rl.Rectangle):
    # 画背景
    rl.draw_rectangle_rounded(rect, 0.1, 20, rl.Color(30, 30, 30, 255))

    # 标题
    title_font = gui_app.font(FontWeight.BOLD)
    title = "网页工具箱"
    title_size = 48
    title_w = measure_text_cached(title_font, title, title_size).x
    title_x = int(rect.x + (rect.width - title_w) / 2)
    title_y = int(rect.y + 20)
    rl.draw_text_ex(title_font, title, rl.Vector2(title_x, title_y), title_size, 0, rl.WHITE)

    # 设备当前 IP（网络变化后自动跟随）
    ip = self._get_device_ip()
    url = f"http://{ip}:5588"

    # 二维码大小
    qr_size = max(1, int(min(rect.width - 80, rect.height - 120)))
    qr_x = int(rect.x + (rect.width - qr_size) / 2)
    qr_y = int(rect.y + 80)

    # 画二维码（纹理按 URL 缓存）
    texture = self._get_qr_texture(url)
    if texture is not None:
      rl.draw_texture_pro(
        texture,
        rl.Rectangle(0, 0, texture.width, texture.height),
        rl.Rectangle(qr_x, qr_y, qr_size, qr_size),
        rl.Vector2(0, 0), 0, rl.WHITE
      )
    else:
      msg_font = gui_app.font(FontWeight.NORMAL)
      msg = "二维码生成失败"
      msg_size = 32
      msg_w = measure_text_cached(msg_font, msg, msg_size).x
      rl.draw_text_ex(
        msg_font, msg,
        rl.Vector2(int(rect.x + (rect.width - msg_w) / 2), int(qr_y + qr_size / 2)),
        msg_size, 0, rl.Color(201, 34, 49, 255)
      )

    # URL 文字
    url_font = gui_app.font(FontWeight.NORMAL)
    url_size = 28
    url_w = measure_text_cached(url_font, url, url_size).x
    url_x = int(rect.x + (rect.width - url_w) / 2)
    url_y = int(qr_y + qr_size + 15)
    rl.draw_text_ex(url_font, url, rl.Vector2(url_x, url_y), url_size, 0, rl.Color(180, 180, 180, 255))

  def _refresh(self):
    self._version_text = self._get_version_text()
    update_available = self.update_alert.refresh()
    alert_count = self.offroad_alert.refresh()
    alerts_present = alert_count > 0

    # Show panels on transition from no alert/update to any alerts/update
    if not update_available and not alerts_present:
      self._set_state(HomeLayoutState.HOME)
    elif update_available and ((not self._prev_update_available) or (not alerts_present and self.current_state == HomeLayoutState.ALERTS)):
      self._set_state(HomeLayoutState.UPDATE)
    elif alerts_present and ((not self._prev_alerts_present) or (not update_available and self.current_state == HomeLayoutState.UPDATE)):
      self._set_state(HomeLayoutState.ALERTS)

    self.update_available = update_available
    self.alert_count = alert_count
    self._prev_update_available = update_available
    self._prev_alerts_present = alerts_present

  def _get_version_text(self) -> str:
    brand = "sunnypilot"
    description = self.params.get("UpdaterCurrentDescription")
    return f"{brand} {description}" if description else brand
