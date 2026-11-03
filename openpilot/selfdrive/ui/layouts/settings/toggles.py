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
  "EnableDashcam": tr_noop(
    "日志录制。<br>"
    "开启后，每次上车接管行驶时，设备会把当时的模型路径、车道线识别、方向盘动作等数据"
    "记录到本机（每 60 秒一段），用于事后排查问题。<br>"
    "本机不联网、不上传，记录只存在设备上，需要时再导出查看；下次上车生效。"
  ),
  "RecordVideo": tr_noop(
    "视频录制。<br>"
    "开启后，在记录行车日志的同时，会把路面摄像头画面也录成视频保存到本机（每 60 秒一段）。<br>"
    "关闭则只记录模型路径、车道线识别、方向盘动作等日志数据，不占用额外存储空间；"
    "修改后下次上车接管时生效。"
  ),
  "IsMetric": tr_noop(
    "公制单位：速度以 km/h 显示；<br>"
    "关闭则以 mph 显示。"
  ),
  "CameraStreamMode": tr_noop(
    "选择驾驶画面使用的镜头。<br>"
    "自动：低速用广角镜头（视野更宽，看得见近处与弯道），高速自动切回长焦镜头（看得更远）。<br>"
    "长焦：始终使用长焦镜头，远处看得更清楚。<br>"
    "广角：始终使用广角镜头，低速与路口视野更宽。<br>"
    "只影响画面显示，不改变驾驶控制。"
  ),
  "CameraSwitchSpeed": tr_noop(
    "自动模式下切换到广角镜头的速度阈值（km/h）。<br>"
    "车速低于此值时使用广角镜头。<br>"
    "默认 28km/h。修改后立即生效。"
  ),
  "CameraRoadSwitchSpeed": tr_noop(
    "自动模式下切换回长焦镜头的速度阈值（km/h）。<br>"
    "车速高于此值时使用长焦镜头。<br>"
    "必须大于等于「广角切换速度」，两个阈值之间就是防抖区间。<br>"
    "默认 32km/h。修改后立即生效。"
  ),
}


# ---- 本功能的状态文件统一放在 /data/carrot_state/（params 目录之外）----
# 为什么不能放 /data/params/d/：system/manager/manager.py 每次开机都会调用
# Params.clearAll()，而 common/params.cc 的 clearAll() 会把参数目录里所有
# 「不在 common/params_keys.h 白名单里」的文件直接 unlink 掉 —— 未登记的自建
# 参数文件因此每重启必被清空、设置回默认值。放在 params 目录之外即可持久化，
# 与 selfdrive/carrot/drive_stats.py 采用同一做法。
CARROT_STATE_DIR = "/data/carrot_state"

# ---- 撞击检测阈值：数值步进控件（单位 g，步长 0.1，范围 0.5~5.0）----
# 后端 selfdrived.py 读取同一文件并 ×9.81 换算成 m/s² 作为阈值
IMPACT_THRESHOLD_FILE = CARROT_STATE_DIR + "/ImpactThreshold"
IMPACT_THRESHOLD_DEFAULT_G = 1.5
IMPACT_THRESHOLD_MIN_G = 0.5
IMPACT_THRESHOLD_MAX_G = 5.0
IMPACT_THRESHOLD_STEP_G = 0.1

_STEP_BTN_W = 80
_STEP_BTN_H = 100
_VALUE_FONT = 50

# ---- 镜头切换：广角/长焦切换速度（单位 km/h，步长 1，范围 5~120）----
# 两个阈值构成迟滞区间：低于「广角切换速度」切广角，高于「长焦切换速度」切长焦，
# 两者之间保持当前镜头，避免在临界速度反复抖动。
CAMERA_SWITCH_SPEED_MIN_KPH = 5
CAMERA_SWITCH_SPEED_MAX_KPH = 120
CAMERA_SWITCH_SPEED_STEP_KPH = 1


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
      os.makedirs(CARROT_STATE_DIR, exist_ok=True)
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


class _CameraSpeedStepper(ItemAction):
  """「-  28km/h  +」步进器：调整镜头切换速度阈值，写指定的 param。

  min_value lets a stepper refuse values below another threshold, so the wide
  and long camera speeds can never be configured in a contradictory order
  (long < wide would make the camera oscillate).
  """

  def __init__(self, param: str, default: int, min_value: int = CAMERA_SWITCH_SPEED_MIN_KPH):
    super().__init__(width=_STEP_BTN_W * 2 + 200)
    self._font = gui_app.font(FontWeight.MEDIUM)
    self._param = param
    self._default = default
    self._min_value = min_value

  def _read_value(self) -> int:
    try:
      value = Params().get_int(self._param)
    except Exception:
      value = self._default
    return max(self._min_value, min(CAMERA_SWITCH_SPEED_MAX_KPH, int(value)))

  def _write_value(self, value: int) -> None:
    try:
      Params().put_int(self._param, int(max(self._min_value, value)))
    except Exception:
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
      (minus_rect, "-", value > CAMERA_SWITCH_SPEED_MIN_KPH),
      (plus_rect, "+", value < CAMERA_SWITCH_SPEED_MAX_KPH),
    ):
      hovered = enabled and rl.check_collision_point_rec(mouse, btn_rect)
      bg = (rl.Color(74, 74, 74, 255) if hovered else rl.Color(57, 57, 57, 255)) if enabled else rl.Color(57, 57, 57, 150)
      rl.draw_rectangle_rounded(btn_rect, 0.9, 20, bg)
      size = measure_text_cached(self._font, label, _VALUE_FONT)
      rl.draw_text_ex(self._font, label, rl.Vector2(btn_rect.x + (btn_rect.width - size.x) / 2,
                                                   btn_rect.y + (btn_rect.height - size.y) / 2),
                      _VALUE_FONT, 0, rl.WHITE if enabled else rl.Color(150, 150, 150, 255))
    text = f"{value}km/h"
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
      self._write_value(max(CAMERA_SWITCH_SPEED_MIN_KPH, value - CAMERA_SWITCH_SPEED_STEP_KPH))
    elif rl.check_collision_point_rec(mouse_pos, plus_rect):
      self._write_value(min(CAMERA_SWITCH_SPEED_MAX_KPH, value + CAMERA_SWITCH_SPEED_STEP_KPH))


class CameraSwitchSpeedAction(_CameraSpeedStepper):
  """切换到广角镜头的速度阈值（CameraSwitchSpeed）。"""

  def __init__(self):
    super().__init__("CameraSwitchSpeed", 28)


class CameraRoadSwitchSpeedAction(_CameraSpeedStepper):
  """切换回长焦镜头的速度阈值（CameraRoadSwitchSpeed）。

  下限跟随「广角切换速度」：长焦阈值不可能低于广角阈值，
  因此两个阈值之间的防抖区间永远存在。
  """

  def __init__(self):
    super().__init__("CameraRoadSwitchSpeed", 32, min_value=self._wide_speed())

  @staticmethod
  def _wide_speed() -> int:
    try:
      value = Params().get_int("CameraSwitchSpeed")
    except Exception:
      value = 28
    return max(CAMERA_SWITCH_SPEED_MIN_KPH, min(CAMERA_SWITCH_SPEED_MAX_KPH, int(value)))


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
      "IsLdwEnabled": (
        lambda: tr("车道偏离预警"),
        DESCRIPTIONS["IsLdwEnabled"],
        "warning.png",
        False,
      ),
      # [日志录制 / 视频录制] 2026-11-01 按用户要求：排在「撞击检测阈值」正下方。
      # 日志录制 = EnableDashcam（沿用原参数，控制 loggerd 进程是否启动，只写 rlog/qlog 日志，不录视频）。
      # 视频录制 = RecordVideo（新增 BOOL 参数，默认关；由 loggerd.h 门控全部视频编码器，含 qcamera.ts）。
      "EnableDashcam": (
        lambda: tr("日志录制"),
        DESCRIPTIONS["EnableDashcam"],
        "dashboard_rec.png",
        False,
      ),
      "RecordVideo": (
        lambda: tr("视频录制"),
        DESCRIPTIONS["RecordVideo"],
        "video_record.png",
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

    # [镜头切换] 2026-11-02 按用户要求：排在「使用公制单位」正下方。
    # 镜头模式 = CameraStreamMode（0 自动 / 1 长焦 / 2 广角）
    # 广角切换速度 = CameraSwitchSpeed（km/h，步进器）
    try:
      camera_mode = self._params.get_int("CameraStreamMode")
    except UnknownKeyName:
      camera_mode = 0
    self._camera_stream_mode_setting = multiple_button_item(
      lambda: tr("镜头切换模式"),
      lambda: tr(DESCRIPTIONS["CameraStreamMode"]),
      buttons=[lambda: tr("自动"), lambda: tr("长焦"), lambda: tr("广角")],
      button_width=170,
      callback=self._set_camera_stream_mode,
      selected_index=camera_mode if 0 <= camera_mode <= 2 else 0,
      icon="speed_limit.png",
    )
    self._camera_switch_speed_item = ListItem(
      title="广角切换速度",
      description=DESCRIPTIONS["CameraSwitchSpeed"],
      icon="speed_limit.png",
      action_item=CameraSwitchSpeedAction(),
    )
    self._camera_road_switch_speed_item = ListItem(
      title="长焦切换速度",
      description=DESCRIPTIONS["CameraRoadSwitchSpeed"],
      icon="speed_limit.png",
      action_item=CameraRoadSwitchSpeedAction(),
    )

    # [撞击检测阈值] 2026-10-29 从「软件」菜单移到本菜单
    self._impact_threshold_item = ListItem(
      title="撞击检测阈值",
      description="冲击或抖动过大时触发「撞击报警」提示的加速度阈值（单位 g）。"
                 "默认 1.5g，范围 0.5~5.0g，超出自动夹取；数值调大更不容易误触发，调小更容易触发。"
                 "设置后立即生效；数值保存在 /data/carrot_state/ImpactThreshold，重启后不会恢复默认。",
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

      # insert longitudinal personality after LDW toggle
      # （原插入点「踩加速踏板退出接管」已按用户要求隐藏，故改挂「车道偏离预警」之后）
      if param == "IsLdwEnabled":
        self._toggles["LongitudinalPersonality"] = self._long_personality_setting

      # [撞击检测阈值] 紧跟在「车道偏离预警」下方显示
      if param == "IsLdwEnabled":
        self._toggles["ImpactThreshold"] = self._impact_threshold_item

      # [镜头切换] 紧跟在「使用公制单位」下方显示
      if param == "IsMetric":
        self._toggles["CameraStreamMode"] = self._camera_stream_mode_setting
        self._toggles["CameraSwitchSpeed"] = self._camera_switch_speed_item
        self._toggles["CameraRoadSwitchSpeed"] = self._camera_road_switch_speed_item

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

    # [日志录制] 动态状态提示：让用户一眼看出「现在到底在不在记」
    dashcam_item = self._toggles.get("EnableDashcam")
    if dashcam_item is not None:
      if self._params.get_bool("EnableDashcam"):
        tip = tr("● 接管中正在记录，退出接管后自动停止。") if ui_state.engaged else tr("已开启，下次上车接管时自动开始记录。")
      else:
        tip = tr("未开启，不会记录任何数据。")
      dashcam_item.set_description(DESCRIPTIONS["EnableDashcam"] + "<br>" + tip)

    # [视频录制] 动态状态提示
    video_item = self._toggles.get("RecordVideo")
    if video_item is not None:
      if self._params.get_bool("RecordVideo"):
        tip = tr("● 已开启，接管时同步录制路面视频。") if ui_state.engaged else tr("已开启，下次上车接管时开始录制视频。")
      else:
        tip = tr("未开启，只记录日志、不录制视频。")
      video_item.set_description(DESCRIPTIONS["RecordVideo"] + "<br>" + tip)

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

  def _set_camera_stream_mode(self, index: int) -> None:
    # CameraStreamMode: 0 自动 / 1 长焦 / 2 广角
    try:
      self._params.put_int("CameraStreamMode", int(index))
    except Exception:
      pass

  def _get_toggle_state(self, param: str) -> bool:
    try:
      return self._params.get_bool(param)
    except UnknownKeyName:
      return False

  def _toggle_callback(self, state: bool, param: str):
    if param == "ExperimentalMode":
      self._handle_experimental_mode_toggle(state)
      return

    self._params.put_bool(param, state)
    if self._toggle_defs[param][3]:
      self._params.put_bool("OnroadCycleRequested", True)

  def _set_longitudinal_personality(self, button_index: int):
    self._params.put("LongitudinalPersonality", button_index)
