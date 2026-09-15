"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.system.ui.widgets import Widget
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.sunnypilot.widgets.list_view import toggle_item_sp, option_item_sp, LineSeparatorSP
from openpilot.system.ui.lib.multilang import tr


class FeaturesLayout(Widget):
  """「功能」菜单：所有新增的独立功能开关统一放在这个面板下。"""

  def __init__(self):
    super().__init__()

    # CP 移植：自动居中 / 弯道居中。
    # 注意：这两个参数是**多态**（自动居中 0=关 1=实时 2=实时+长期学习），
    # 必须用 option_item_sp。原来用 toggle_item_sp 会被 put_bool 写成 0/1，
    # 把用户设的 2 静默降级成 1。
    self._auto_centering_item = option_item_sp(
      title=lambda: tr("自动居中纠偏"),
      param="AutoCenterMode",
      min_value=0,
      max_value=2,
      value_change_step=1,
      description=lambda: tr("0=关闭；1=实时纠偏；2=实时纠偏加长期学习。数值越大介入越强。"),
    )
    self._curve_centering_item = option_item_sp(
      title=lambda: tr("弯道居中"),
      param="CurveCenterMode",
      min_value=0,
      max_value=1,
      value_change_step=1,
      description=lambda: tr("开启后，在弯道中优先保持车道居中，提升过弯横向稳定性。"),
    )

    # CP 移植：视觉弯道限速（复用 SP 原生 Smart Cruise Control - Vision 的物理算法，
    # 新增「激进程度」与「最低速度下限」两个上游没有的可调项）
    self._vision_turn_toggle = toggle_item_sp(
      param="VisionTurnSpeedEnabled",
      title=lambda: tr("视觉弯道限速"),
      description=lambda: tr("开启后，根据驾驶模型预测的轨迹估算过弯所需的速度，提前主动减速。"
                             "与「弯道激进程度」和「自动弯道速度下限」配合使用。"),
    )
    self._turn_aggr_item = option_item_sp(
      title=lambda: tr("弯道激进程度"),
      param="TurnSpeedAggressiveness",
      min_value=50,
      max_value=150,
      value_change_step=5,
      description=lambda: tr("视觉弯道限速的强度。100 为标准；小于 100 过弯更慢更保守，大于 100 过弯更快。"),
    )
    self._curve_min_speed_item = option_item_sp(
      title=lambda: tr("自动弯道速度下限"),
      param="AutoCurveSpeedLowerLimit",
      min_value=0,
      max_value=60,
      value_change_step=5,
      description=lambda: tr("弯道中允许的最低速度（km/h）。防止转弯被压得过慢或停在半路。0=不限。"),
    )

    # CP 移植：跟车停车距离（运行期可调，出厂基准 6 米）
    self._stop_distance_item = option_item_sp(
      title=lambda: tr("停车距离"),
      param="StopDistance",
      min_value=200,
      max_value=1500,
      value_change_step=50,
      use_float_scaling=True,
      description=lambda: tr("跟车停止时与前车保持的目标距离（米）。数值越大停得越远，出厂基准为 6 米。"),
    )

    self._lane_path_color_item = option_item_sp(
      title=lambda: tr("车道线轨迹颜色"),
      param="ShowPathColorLane",
      min_value=0,
      max_value=20,
      value_change_step=1,
      description=lambda: tr("识别到左右车道线时，把行驶轨迹换成指定颜色。0=关闭；"
                             "1-10:红 橙 黄 绿 蓝 深蓝 紫 棕 白 黑；11-20:同色并带描边。"),
    )
    self._wide_cam_speed_item = option_item_sp(
      title=lambda: tr("广角切换速度"),
      param="WideCamSpeedKph",
      min_value=0,
      max_value=200,
      value_change_step=5,
      description=lambda: tr("实验模式下，车速低于此值自动切到广角摄像头（km/h）。0=禁用广角切换。"),
    )
    self._tele_cam_speed_item = option_item_sp(
      title=lambda: tr("长焦切换速度"),
      param="TeleCamSpeedKph",
      min_value=0,
      max_value=200,
      value_change_step=5,
      description=lambda: tr("实验模式下，车速高于此值自动切回长焦（标准）摄像头（km/h）。0=禁用该切换。"),
    )

    # CP 移植：红绿灯/停止标志虚拟停止线
    self._traffic_stop_toggle = toggle_item_sp(
      param="TrafficStopEnabled",
      title=lambda: tr("红绿灯/停止标志停等"),
      description=lambda: tr("用驾驶模型自己预测的轨迹判断车辆是否正在趋向停止，在轨迹终点放一个虚拟静止障碍物，"
                             "让车像跟一台停在那里的车一样自然煞停。不需要额外的红绿灯识别模型、地图或导航。"
                             "出厂默认关闭；开启后只在你已接管纵向控制时生效。"),
    )
    self._traffic_stop_adjust = option_item_sp(
      title=lambda: tr("停等距离微调"),
      param="TrafficStopDistanceAdjust",
      min_value=-500,
      max_value=500,
      value_change_step=10,
      use_float_scaling=True,
      description=lambda: tr("微调停等位置（米）。正值=停得更早、离停止线更远；负值=更靠近停止线。"
                             "默认 0 已经包含固定的相机安装物理修正，通常不需要调。"),
    )

    items = [
      self._auto_centering_item,
      LineSeparatorSP(40),
      self._curve_centering_item,
      LineSeparatorSP(40),
      self._vision_turn_toggle,
      LineSeparatorSP(40),
      self._turn_aggr_item,
      LineSeparatorSP(40),
      self._curve_min_speed_item,
      LineSeparatorSP(40),
      self._stop_distance_item,
      LineSeparatorSP(40),
      self._lane_path_color_item,
      LineSeparatorSP(40),
      self._wide_cam_speed_item,
      LineSeparatorSP(40),
      self._tele_cam_speed_item,
      LineSeparatorSP(40),
      self._traffic_stop_toggle,
      LineSeparatorSP(40),
      self._traffic_stop_adjust,
    ]
    self._scroller = Scroller(items, line_separator=False, spacing=0)

  def _update_state(self):
    super()._update_state()

  def _render(self, rect):
    self._scroller.render(rect)

  def show_event(self):
    self._scroller.show_event()
