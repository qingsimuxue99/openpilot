"""
录屏开关按钮（悬在 eGPU 铭牌上方）

样式沿用旧版 CP 的 Qt ScreenRecoder：
  - 半透明圆底（旧版 QColor::fromRgbF(0,0,0,0.3)，此处按方案 C 进一步淡化）
  - 录制时转红并每 0.5 秒明暗交替（旧版 applyColor 的节奏）
  - 旧版只有光秃秃的圆点，这里补一个 ⏺ 录制符号，便于识别

点击即开始/停止录屏（走 application.py 的 start_recording / stop_recording，
即硬件 H264 录屏链路）。
"""
import pyray as rl

from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets import Widget


class RecButton(Widget):
  """录屏按钮。diameter 为圆直径，绘制时以 rect 中心为圆心。"""

  def __init__(self, diameter: int = 76):
    super().__init__()
    self._diameter = int(diameter)

  def _handle_mouse_release(self, _) -> None:
    super()._handle_mouse_release(_)
    recording = gui_app.is_recording()
    if recording:
      gui_app.stop_recording()
    else:
      gui_app.start_recording()
    # 同步 ScreenRecord 参数，让网页端/语音助手看到同样的状态
    try:
      from openpilot.selfdrive.ui.ui_state import ui_state
      ui_state.params.put_bool_nonblocking("ScreenRecord", not recording)
    except Exception:
      pass

  def _render(self, rect: rl.Rectangle) -> None:
    # pyray 的几何接口只接受整数，float 会直接抛 TypeError 把 UI 打死
    r = int(rect.width / 2)
    cx = int(rect.x + rect.width / 2)
    cy = int(rect.y + rect.height / 2)

    recording = gui_app.is_recording()
    # 旧版：每 0.5 秒在亮/暗之间交替
    blink = int(rl.get_time() * 2) % 2 == 0

    if recording:
      # 录制中：红色满亮 + 外圈加亮，一眼就能看出正在录
      bg = rl.Color(0, 0, 0, 26) if blink else rl.Color(122, 16, 16, 77)
      ring = rl.Color(255, 255, 255, 140 if blink else 90)
      icon = rl.Color(255, 75, 75, 255 if blink else 190)
    else:
      # 未录制：整体压暗。原来图标是 alpha 240（几乎全白），
      # 比外圈亮 3 倍，整颗按钮像贴了张白纸上去，很抢眼。
      # 这里把图标压到 95、外圈 40、底色 13，只留一个隐约轮廓，
      # 需要的时候看得见，但不干扰路面信息。
      bg = rl.Color(0, 0, 0, 13)
      ring = rl.Color(255, 255, 255, 40)
      icon = rl.Color(255, 255, 255, 95)

    rl.draw_circle(cx, cy, r, bg)
    rl.draw_circle_lines(cx, cy, r - 1, ring)

    # 图标：外圈圆环 + 中心符号（录制中=实心圆点，停止=方块）
    rl.draw_circle_lines(cx, cy, int(r * 0.52), icon)
    if recording:
      rl.draw_circle(cx, cy, int(r * 0.20), icon)
    else:
      s = max(2, int(r * 0.30))
      rl.draw_rectangle(cx - s // 2, cy - s // 2, s, s, icon)
