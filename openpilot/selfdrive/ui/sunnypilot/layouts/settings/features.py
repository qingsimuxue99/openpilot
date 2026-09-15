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

    self._auto_centering_toggle = toggle_item_sp(
      param="AutoCenteringCorrection",
      title=lambda: tr("自动居中纠偏"),
      description=lambda: tr("开启后，系统将持续把车辆自动纠偏回车道中心，抑制横向漂移。"),
    )
    self._curve_centering_toggle = toggle_item_sp(
      param="CurveCentering",
      title=lambda: tr("弯道居中"),
      description=lambda: tr("开启后，在弯道中优先保持车道居中，提升过弯横向稳定性。"),
    )

    # CP 移植：自动开启巡航（已移除，功能无效）

    # CP 移植：车道线轨迹颜色 + 广角/长焦摄像头切换速度
    # 转弯最低速度
    self._curve_min_speed_item = option_item_sp(
      title=lambda: tr("转弯最低速度"),
      param="CurveMinSpeed",
      min=10, max=60, step=5, unit="km/h",
      description=lambda: tr("转弯时不低于此速度，防止转弯太慢或停半路。0=不限。"),
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
      description=lambda: tr("微调停等位置（米）。正值＝停得更早、离停止线更远；负值＝更靠近停止线。"
                             "默认 0 已经包含固定的相机安装物理修正，通常不需要调。"),
    )

    items = [
      self._auto_centering_toggle,
      LineSeparatorSP(40),
      self._curve_centering_toggle,
      LineSeparatorSP(40),
      self._auto_gas_tok_speed,
      LineSeparatorSP(40),
      self._cruise_on_dist,
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
