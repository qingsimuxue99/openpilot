import os

import pyray as rl

from openpilot.cereal import log
from openpilot.common.params import Params, UnknownKeyName
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.list_view import (multiple_button_item, toggle_item, ListItem, ItemAction,
                                                   ITEM_TEXT_VALUE_COLOR)
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.multilang import tr, tr_noop
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import DialogResult
from openpilot.selfdrive.ui.ui_state import ui_state

PERSONALITY_TO_INT = log.LongitudinalPersonality.schema.enumerants

# 各参数的说明文案（2026-10-17 全部中文化，按用户要求补全细节）
#
# ⚠️ 换行说明：设备 UI 的 `label.py` **没有实现自动换行**（源码里留着
#   "TODO: add wrapping"），超宽文本会被 elide 截断成省略号。
#   ListItem 的描述默认隐藏，点条目才展开，展开时走 HtmlRenderer 支持 <br>。
#   ⇒ 因此长描述必须**手动用 <br> 分行**，否则会被截断显示不全。
DESCRIPTIONS = {
  "OpenpilotEnabledToggle": tr_noop(
    "开启后由萝卜接管自适应巡航与车道保持。<br>"
    "使用该功能时必须全程保持注意力，随时准备接管车辆，系统不会替你观察路况。"
  ),
  "DisengageOnAccelerator": tr_noop(
    "开启后踩下加速踏板会立即退出萝卜接管，便于手动超车。<br>"
    "关闭后踩加速踏板不会退出接管，纵向仍由系统控制。"
  ),
  "LongitudinalPersonality": tr_noop(
    "标准（推荐）：最均衡，适合日常使用。<br>"
    "激进：跟车更近，加减速更果断。<br>"
    "放松：跟车距离更远。<br>"
    "更松：跟车距离最远。<br>"
    "支持的车型可用方向盘上的距离按键循环切换。"
  ),
  "IsLdwEnabled": tr_noop(
    "车道偏离预警。<br>"
    "车速超过 50 km/h 且未打转向灯时，若车辆越过识别到的车道线，将提醒你回正方向盘。"
  ),
  "ForceCenter": tr_noop(
    "车道线强制居中。<br>"
    "开启后，在能同时识别到左右两侧车道线时，强制将车辆保持在两条车道线的几何正中央，"
    "忽略单侧标线更清晰带来的偏移，解决持续偏向某一侧的问题。<br>"
    "仅在两侧车道线均可见时生效；无车道线的乡村道路自动退回原逻辑，不影响正常行驶。"
  ),
  "AlwaysOnDM": tr_noop(
    "驾驶员监控常开。<br>"
    "即使萝卜未接管，也持续进行疲劳与分心监测（仅部分车型支持）。"
  ),
  'RecordFront': tr_noop(
    "记录并上传驾驶员摄像头画面，用于改进驾驶员监控算法。<br>"
    "开启前请确认你了解并同意相关隐私政策。"
  ),
  "IsMetric": tr_noop(
    "公制单位：速度以 km/h 显示；<br>"
    "关闭则以 mph 显示。"
  ),
  "RecordAudio": tr_noop(
    "记录并上传麦克风音频，<br>"
    "会一并写入行车记录视频（仅在联网记录时生效）。"
  ),
}


# ---- 撞击检测阈值：数值步进控件（单位 g，步长 0.1，范围 0.5~5.0）----
# 直写原始文件 /data/params/d/ImpactThreshold（绕过 params 白名单，与 DisableUpdates 同机制）
# 后端 selfdrived.py 读取该文件并 ×9.81 换算成 m/s² 作为阈值
IMPACT_THRESHOLD_FILE = "/data/params/d/ImpactThreshold"
IMPACT_THRESHOLD_DEFAULT_G = 1.5
IMPACT_THRESHOLD_MIN_G = 0.5
IMPACT_THRESHOLD_MAX_G = 5.0
IMPACT_THRESHOLD_STEP_G = 0.1

_STEP_BTN_W = 80
_STEP_BTN_H = 100
_VALUE_FONT = 50


class ImpactThresholdAction(ItemAction):
  """「-  1.5g  +」步进器：点击 ± 调整撞击检测阈值（单位 g），写原始参数文件。"""

  def __init__(self):
    super().__init__(width=_STEP_BTN_W * 2 + 160)
    self._font = gui_app.font(FontWeight.MEDIUM)

  @staticmethod
  def _read_value() -> float:
    try:
      with open(IMPACT_THRESHOLD_FILE, "rb") as f:
        return max(IMPACT_THRESHOLD_MIN_G, min(IMPACT_THRESHOLD_MAX_G, float(f.read().strip())))
    except (OSError, ValueError):
      return IMPACT_THRESHOLD_DEFAULT_G

  @staticmethod
  def _write_value(value: float) -> None:
    try:
      with open(IMPACT_THRESHOLD_FILE, "wb") as f:
        f.write(f"{value:.1f}".encode())
    except OSError:
      pass

  def _btn_rects(self):
    y = self._rect.y + (self._rect.height - _STEP_BTN_H) / 2
    minus = rl.Rectangle(self._rect.x, y, _STEP_BTN_W, _STEP_BTN_H)
    plus = rl.Rectangle(self._rect.x + self._rect.width - _STEP_BTN_W, y, _STEP_BTN_W, _STEP_BTN_H)
    return minus, plus

  def _render(self, rect):
    minus_rect, plus_rect = self._btn_rects()
    value = self._read_value()
    mouse = rl.get_mouse_position()
    for btn_rect, label, enabled in (
      (minus_rect, "-", value > IMPACT_THRESHOLD_MIN_G),
      (plus_rect, "+", value < IMPACT_THRESHOLD_MAX_G),
    ):
      hovered = enabled and rl.check_collision_point_rec(mouse, btn_rect)
      bg = (rl.Color(74, 74, 74, 255) if hovered else rl.Color(57, 57, 57, 255)) if enabled else rl.Color(57, 57, 57, 150)
      rl.draw_rectangle_rounded(btn_rect, 0.9, 20, bg)
      size = measure_text_cached(self._font, label, _VALUE_FONT)
      rl.draw_text_ex(self._font, label, rl.Vector2(btn_rect.x + (btn_rect.width - size.x) / 2,
                                                   btn_rect.y + (btn_rect.height - size.y) / 2),
                      _VALUE_FONT, 0, rl.WHITE if enabled else rl.Color(150, 150, 150, 255))
    text = f"{value:.1f}g"
    size = measure_text_cached(self._font, text, _VALUE_FONT)
    rl.draw_text_ex(self._font, text, rl.Vector2(minus_rect.x + _STEP_BTN_W + 8,
                                                rect.y + (rect.height - size.y) / 2),
                    _VALUE_FONT, 0, ITEM_TEXT_VALUE_COLOR)
    return False

  def _handle_mouse_release(self, mouse_pos):
    if not self.enabled:
      return
    minus_rect, plus_rect = self._btn_rects()
    value = self._read_value()
    if rl.check_collision_point_rec(mouse_pos, minus_rect):
      new = max(IMPACT_THRESHOLD_MIN_G, round((value - IMPACT_THRESHOLD_STEP_G) / IMPACT_THRESHOLD_STEP_G) * IMPACT_THRESHOLD_STEP_G)
      self._write_value(round(new, 1))
    elif rl.check_collision_point_rec(mouse_pos, plus_rect):
      new = min(IMPACT_THRESHOLD_MAX_G, round((value + IMPACT_THRESHOLD_STEP_G) / IMPACT_THRESHOLD_STEP_G) * IMPACT_THRESHOLD_STEP_G)
      self._write_value(round(new, 1))


class TogglesLayout(Widget):
  def __init__(self):
    super().__init__()
    self._params = Params()
    self._is_release = self._params.get_bool("IsReleaseBranch")

    # param, title, desc, icon, needs_restart
    self._toggle_defs = {
      "OpenpilotEnabledToggle": (
        lambda: tr("启用萝卜接管"),
        DESCRIPTIONS["OpenpilotEnabledToggle"],
        "chffr_wheel.png",
        True,
      ),
      "ExperimentalMode": (
        lambda: tr("实验模式"),
        "",
        "experimental_white.png",
        False,
      ),
      "DisengageOnAccelerator": (
        lambda: tr("踩加速踏板退出接管"),
        DESCRIPTIONS["DisengageOnAccelerator"],
        "disengage_on_accelerator.png",
        False,
      ),
      "IsLdwEnabled": (
        lambda: tr("车道偏离预警"),
        DESCRIPTIONS["IsLdwEnabled"],
        "warning.png",
        False,
      ),
      "ForceCenter": (
        lambda: tr("车道线强制居中"),
        DESCRIPTIONS["ForceCenter"],
        "center_lane.png",
        False,
      ),
      "AlwaysOnDM": (
        lambda: tr("驾驶员监控常开"),
        DESCRIPTIONS["AlwaysOnDM"],
        "monitoring.png",
        False,
      ),
      "RecordFront": (
        lambda: tr("记录并上传驾驶员画面"),
        DESCRIPTIONS["RecordFront"],
        "monitoring.png",
        True,
      ),
      "RecordAudio": (
        lambda: tr("记录并上传麦克风音频"),
        DESCRIPTIONS["RecordAudio"],
        "microphone.png",
        True,
      ),
      "IsMetric": (
        lambda: tr("使用公制单位"),
        DESCRIPTIONS["IsMetric"],
        "metric.png",
        False,
      ),
    }

    self._long_personality_setting = multiple_button_item(
      lambda: tr("驾驶风格"),
      lambda: tr(DESCRIPTIONS["LongitudinalPersonality"]),
      buttons=[lambda: tr("激进"), lambda: tr("标准"), lambda: tr("放松"), lambda: tr("更松")],
      button_width=255,
      callback=self._set_longitudinal_personality,
      selected_index=self._params.get("LongitudinalPersonality", return_default=True),
      icon="speed_limit.png"
    )

    # [撞击检测阈值] 2026-10-29 从「软件」菜单移到本菜单，紧跟「车道线强制居中」下方
    self._impact_threshold_item = ListItem(
      title="撞击检测阈值",
      description="冲击或抖动过大时触发「撞击报警」提示的加速度阈值（单位 g）。"
                 "默认 1.5g，范围 0.5~5.0g，超出自动夹取；数值调大更不容易误触发，调小更容易触发。"
                 "也可直接写 /data/params/d/ImpactThreshold 覆盖，约 2 秒后生效。",
      icon="warning.png",
      action_item=ImpactThresholdAction(),
    )

    self._toggles = {}
    self._locked_toggles = set()
    for param, (title, desc, icon, needs_restart) in self._toggle_defs.items():
      toggle = toggle_item(
        title,
        desc,
        self._get_toggle_state(param),
        callback=lambda state, p=param: self._toggle_callback(state, p),
        icon=icon,
      )

      try:
        locked = self._params.get_bool(param + "Lock")
      except UnknownKeyName:
        locked = False
      toggle.action_item.set_enabled(not locked)

      # Make description callable for live translation
      additional_desc = ""
      if needs_restart and not locked:
        additional_desc = tr("修改该设置后，若车辆已上电，将自动重启萝卜系统使配置生效。")
      toggle.set_description(lambda og_desc=toggle.description, add_desc=additional_desc: tr(og_desc) + (" " + tr(add_desc) if add_desc else ""))

      # track for engaged state updates
      if locked:
        self._locked_toggles.add(param)

      self._toggles[param] = toggle

      # insert longitudinal personality after NDOG toggle
      if param == "DisengageOnAccelerator":
        self._toggles["LongitudinalPersonality"] = self._long_personality_setting

      # [撞击检测阈值] 紧跟在「车道线强制居中」下方显示
      if param == "ForceCenter":
        self._toggles["ImpactThreshold"] = self._impact_threshold_item

    self._update_experimental_mode_icon()
    self._scroller = Scroller(list(self._toggles.values()), line_separator=True, spacing=0)

    ui_state.add_engaged_transition_callback(self._update_toggles)

  def _update_state(self):
    if ui_state.sm.updated["selfdriveState"]:
      personality = PERSONALITY_TO_INT[ui_state.sm["selfdriveState"].personality]
      if personality != ui_state.personality and ui_state.started:
        self._long_personality_setting.action_item.set_selected_button(personality)
      ui_state.personality = personality

  def show_event(self):
    super().show_event()
    self._scroller.show_event()
    self._update_toggles()

  def _update_toggles(self):
    ui_state.update_params()

    e2e_description = tr(
      "萝卜默认以「平稳模式」行驶。实验模式会开启尚未达到平稳模式成熟度的 alpha 级功能，"
      "具体如下：<br>" +
      "<h4>端到端纵向控制</h4><br>" +
      "由驾驶模型直接控制油门与刹车，像人类一样驾驶，包括红灯停车、停车让行。"
      "由于速度由驾驶模型决定，设定速度仅作为上限。这是 alpha 阶段的功能，仍可能有失误。<br>" +
      "<h4>全新驾驶可视化</h4><br>" +
      "低速时驾驶画面会切换到路向前视广角摄像头，以更好呈现部分弯道；"
      "屏幕右上角会同时显示实验模式标识。"
    )

    if ui_state.CP is not None:
      if ui_state.has_longitudinal_control:
        self._toggles["ExperimentalMode"].action_item.set_enabled(True)
        self._toggles["ExperimentalMode"].set_description(e2e_description)
        self._long_personality_setting.action_item.set_enabled(True)
      else:
        # no long for now
        self._toggles["ExperimentalMode"].action_item.set_enabled(False)
        self._toggles["ExperimentalMode"].action_item.set_state(False)
        #self._long_personality_setting.action_item.set_enabled(False)
        self._params.remove("ExperimentalMode")

        unavailable = tr("本车纵向控制使用原车 ACC，实验模式当前不可用。")

        long_desc = unavailable + " " + tr("萝卜的纵向控制能力将在后续更新中提供。")
        if ui_state.CP.alphaLongitudinalAvailable:
          if self._is_release:
            long_desc = unavailable + " " + tr("在非发布分支上，可与实验模式一起测试萝卜纵向控制的 alpha 版本。")
          else:
            long_desc = tr("请先开启「萝卜纵向控制（alpha）」开关，才能使用实验模式。")

        self._toggles["ExperimentalMode"].set_description("<b>" + long_desc + "</b><br>" + e2e_description)
    else:
      self._toggles["ExperimentalMode"].set_description(e2e_description)

    self._update_experimental_mode_icon()

    # TODO: make a param control list item so we don't need to manage internal state as much here
    # refresh toggles from params to mirror external changes
    for param in self._toggle_defs:
      self._toggles[param].action_item.set_state(self._get_toggle_state(param))

    # these toggles need restart, block while engaged
    for toggle_def in self._toggle_defs:
      if self._toggle_defs[toggle_def][3] and toggle_def not in self._locked_toggles:
        self._toggles[toggle_def].action_item.set_enabled(not ui_state.engaged)

  def _render(self, rect):
    self._scroller.render(rect)

  def _update_experimental_mode_icon(self):
    icon = "experimental.png" if self._toggles["ExperimentalMode"].action_item.get_state() else "experimental_white.png"
    self._toggles["ExperimentalMode"].set_icon(icon)

  def _handle_experimental_mode_toggle(self, state: bool):
    confirmed = self._params.get_bool("ExperimentalModeConfirmed")
    if state and not confirmed:
      def confirm_callback(result: DialogResult):
        if result == DialogResult.CONFIRM:
          self._params.put_bool("ExperimentalMode", True)
          self._params.put_bool("ExperimentalModeConfirmed", True)
        else:
          self._toggles["ExperimentalMode"].action_item.set_state(False)
        self._update_experimental_mode_icon()

      # show confirmation dialog
      content = (f"<h1>{self._toggles['ExperimentalMode'].title}</h1><br>" +
                 f"<p>{self._toggles['ExperimentalMode'].description}</p>")
      dlg = ConfirmDialog(content, tr("开启"), rich=True, callback=confirm_callback)
      gui_app.push_widget(dlg)
    else:
      self._update_experimental_mode_icon()
      self._params.put_bool("ExperimentalMode", state)

  # [车道线强制居中] 状态绕过参数白名单，直接读写 /data/params/d/ForceCenter 文件
  def _force_center_state(self) -> bool:
    try:
      with open("/data/params/d/ForceCenter", "rb") as f:
        return f.read().strip() not in (b"0", b"", b"false", b"False")
    except OSError:
      return False

  def _set_force_center_state(self, state: bool):
    os.makedirs("/data/params/d", exist_ok=True)
    with open("/data/params/d/ForceCenter", "wb") as f:
      f.write(b"1" if state else b"0")

  def _get_toggle_state(self, param: str) -> bool:
    if param == "ForceCenter":
      return self._force_center_state()
    try:
      return self._params.get_bool(param)
    except UnknownKeyName:
      return False

  def _toggle_callback(self, state: bool, param: str):
    if param == "ForceCenter":
      self._set_force_center_state(state)
      return
    if param == "ExperimentalMode":
      self._handle_experimental_mode_toggle(state)
      return

    self._params.put_bool(param, state)
    if self._toggle_defs[param][3]:
      self._params.put_bool("OnroadCycleRequested", True)

  def _set_longitudinal_personality(self, button_index: int):
    self._params.put("LongitudinalPersonality", button_index)
