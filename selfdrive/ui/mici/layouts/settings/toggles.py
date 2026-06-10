从 cereals 导入 log

来自openpilot。系统。ui。小部件滚动条 导入NavScroller
从openpilot.selfdrive.ui.mici.widgets.button 导入BigParamControl, BigMultiParamToggle
从openpilot.system.ui.lib.application 导入gui_app
从openpilot.selfdrive.ui.layouts.settings.common 导入restart_needed_callback
从openpilot.自驾.ui.ui_state 导入ui_state

PERSONALITY_TO_INT = log.LongitudinalPersonality.schema.枚举值


类TogglesLayoutMici(NavScroller):
   __init__(self):
    super().__init__()

    self._personality_toggle = BigMultiParamToggle("驾驶个性", "纵向个性", ["激进", "标准", "放松", "非常放松"])
    self._experimental_btn = BigParamControl("实验模式", "ExperimentalMode")
    is_metric_toggle = BigParamControl("使用公制单位", "IsMetric")
    ldw_toggle = BigParamControl("车道偏离警告", "IsLdwEnabled")
    always_on_dm_toggle = BigParamControl("始终开启的驱动程序监控", "AlwaysOnDM")
    record_front = BigParamControl("录制并上传驾驶员摄像头", "RecordFront", toggle_callback=restart_needed_callback)
    record_mic = BigParamControl("录制并上传麦克风音频", "RecordAudio", toggle_callback=restart_needed_callback)
    enable_openpilot = BigParamControl("enable sunnypilot", "OpenpilotEnabledToggle", toggle_callback=restart_needed_callback)

    self._scroller.add_widgets([
      self._personality_toggle,
      self._experimental_btn,
      is_metric_toggle,
      ldw_toggle,
      always_on_dm_toggle,
      前置记录,
      麦克风记录,
      启用OpenPilot,
    ])

    # Toggle lists
    self._refresh_toggles = (
      ("ExperimentalMode", self._experimental_btn),
      ("IsMetric", is_metric_toggle),
      ("IsLdwEnabled", ldw_toggle),
      ("AlwaysOnDM", always_on_dm_toggle),
      ("RecordFront", record_front),
      ("RecordAudio", record_mic),
      ("OpenpilotEnabledToggle", enable_openpilot),
    )

    enable_openpilot.set_enabled(lambda: not ui_state.engaged)
    record_front.set_enabled(False if ui_state.params.get_bool("RecordFrontLock") else (lambda: not ui_state.engaged))
    record_mic.set_enabled(lambda: not ui_state.engaged)

    if ui_state.params.get_bool("ShowDebugInfo"):
      gui_app.set_show_touches(True)
      gui_app.set_show_fps(True)

    ui_state.add_engaged_transition_callback(self._update_toggles)

  def _update_state(self):
    super()._update_state()

    if ui_state.sm.updated["selfdriveState"]:
      personality = PERSONALITY_TO_INT[ui_state.sm["selfdriveState"].personality]
      if personality != ui_state.personality and ui_state.started:
        self._personality_toggle.set_value(self._personality_toggle._options[personality])
      ui_state.personality = personality

  def show_event(self):
    super().show_event()
    self._update_toggles()

  def _update_toggles(self):
    ui_state.update_params()

    # CP gating for experimental mode
    if ui_state.CP is not None:
      if ui_state.has_longitudinal_control:
        self._experimental_btn.set_visible(True)
        self._personality_toggle.set_visible(True)
      else:
        # no long for now
        self._experimental_btn.set_visible(False)
        self._experimental_btn.set_checked(False)
        self._personality_toggle.set_visible(False)
        ui_state.params.remove("ExperimentalMode")

    # Refresh toggles from params to mirror external changes
    for key, item in self._refresh_toggles:
      item.set_checked(ui_state.params.get_bool(key))
