import os
import time
import datetime
from collections.abc import Callable

import pyray as rl

from openpilot.common.time_helpers import system_time_valid
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight, MousePos
from openpilot.system.ui.lib.multilang import tr, trn
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget, DialogResult
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.list_view import (ITEM_PADDING, ITEM_TEXT_FONT_SIZE, ITEM_TEXT_VALUE_COLOR,
                                                   ListItem, TextAction, ItemAction, button_item, toggle_item)
from openpilot.system.ui.widgets.option_dialog import MultiOptionDialog
from openpilot.system.ui.widgets.scroller_tici import Scroller

# TODO: remove this. updater fails to respond on startup if time is not correct
UPDATED_TIMEOUT = 10  # seconds to wait for updated to respond

# 快速启动：预编译标记文件。launch_chffrplus.sh 见标即跳过一次开机编译（[ ! -f $DIR/prebuilt ]）
PREBUILT_MARKER = "/data/openpilot/prebuilt"
# 禁止更新：updated 进程总闸参数（等价命令 echo -en "1" > /data/params/d/DisableUpdates）
DISABLE_UPDATES_PARAM = "/data/params/d/DisableUpdates"


def quick_start_enabled() -> bool:
  """预编译标记是否存在（存在 = 开机跳过编译）"""
  return os.path.exists(PREBUILT_MARKER)


def disable_updates_enabled() -> bool:
  """DisableUpdates 参数是否为 1"""
  try:
    with open(DISABLE_UPDATES_PARAM, "rb") as f:
      return f.read().strip() == b"1"
  except OSError:
    return False

# Mapping updater internal states to translated display strings
STATE_TO_DISPLAY_TEXT = {
  "checking...": tr("checking..."),
  "downloading...": tr("downloading..."),
  "finalizing update...": tr("finalizing update..."),
}


def time_ago(date: datetime.datetime | None) -> str:
  if not date:
    return tr("never")

  if not system_time_valid():
    return date.strftime("%a %b %d %Y")

  now = datetime.datetime.now(datetime.UTC)
  if date.tzinfo is None:
    date = date.replace(tzinfo=datetime.UTC)

  diff_seconds = int((now - date).total_seconds())
  if diff_seconds < 60:
    return tr("now")
  if diff_seconds < 3600:
    m = diff_seconds // 60
    return trn("{} minute ago", "{} minutes ago", m).format(m)
  if diff_seconds < 86400:
    h = diff_seconds // 3600
    return trn("{} hour ago", "{} hours ago", h).format(h)
  if diff_seconds < 604800:
    d = diff_seconds // 86400
    return trn("{} day ago", "{} days ago", d).format(d)
  return date.strftime("%a %b %d %Y")


class VersionTapItem(ListItem):
  """「当前版本」行：连点标题 6 次才显示卸载按钮（防止误卸载）"""

  TAPS_TO_REVEAL = 6

  def __init__(self, *args, on_reveal: Callable[[], None] | None = None, **kwargs):
    super().__init__(*args, **kwargs)
    self._tap_count = 0
    self._on_reveal = on_reveal

  def reset_taps(self) -> None:
    """连点计数清零（切换出软件菜单时调用，下次进入需重新连点）"""
    self._tap_count = 0

  def _handle_mouse_release(self, mouse_pos: MousePos) -> None:
    if not self.is_visible:
      return

    # 右侧控件区（版本值文本）不计数
    if self.action_item:
      action_rect = self.get_right_item_rect(self._rect)
      if rl.check_collision_point_rec(mouse_pos, action_rect):
        return

    # 仅「当前版本」标题区域内的点击计入连点
    title_width = measure_text_cached(self._font, self.title, ITEM_TEXT_FONT_SIZE).x
    title_rect = rl.Rectangle(self._rect.x + ITEM_PADDING, self._rect.y, title_width, self._rect.height)
    if rl.check_collision_point_rec(mouse_pos, title_rect):
      self._tap_count += 1
      if self._tap_count >= self.TAPS_TO_REVEAL:
        self._tap_count = 0
        if self._on_reveal is not None:
          self._on_reveal()
      return

    # 标题以外区域：维持原有行为（展开/收起版本说明）
    self._set_description_visible(not self.description_visible)


class SoftwareLayout(Widget):
  def __init__(self):
    super().__init__()

    self._onroad_label = ListItem(lambda: tr("Updates are only downloaded while the car is off."))

    # 卸载按钮默认隐藏：连点「当前版本」标题 6 次才显示（防止误卸载）
    self._uninstall_btn = button_item(lambda: tr("Uninstall"), lambda: tr("UNINSTALL"), callback=self._on_uninstall)
    self._uninstall_btn.set_visible(False)

    # 「当前版本」行：标题连点 6 次才显示卸载按钮
    self._version_item = VersionTapItem(
      title=lambda: tr("Current Version"),
      action_item=TextAction(ui_state.params.get("UpdaterCurrentDescription") or "", color=ITEM_TEXT_VALUE_COLOR),
      on_reveal=self._reveal_uninstall,
    )

    # 快速启动 / 禁止更新：显示状态分别取自预编译标记文件与 DisableUpdates 参数文件
    self._quick_start_toggle = toggle_item(
      "快速启动",
      "开启后创建预编译标记，开机跳过编译立即启动，并立即重启生效；关闭后删除标记，恢复开机编译。",
      initial_state=quick_start_enabled(),
      callback=self._on_quick_start_toggle,
    )
    self._disable_updates_toggle = toggle_item(
      "禁止更新",
      "开启后设备不再自动更新（写入 DisableUpdates 参数），重启后生效。",
      initial_state=disable_updates_enabled(),
      callback=self._on_disable_updates_toggle,
    )

    self._download_btn = button_item(lambda: tr("Download"), lambda: tr("CHECK"), callback=self._on_download_update)

    # Install button is initially hidden
    self._install_btn = button_item(lambda: tr("Install Update"), lambda: tr("INSTALL"), callback=self._on_install_update)
    self._install_btn.set_visible(False)

    # Track waiting-for-updater transition to avoid brief re-enable while still idle
    self._waiting_for_updater = False
    self._waiting_start_ts: float = 0.0

    # Branch switcher
    self._branch_btn = button_item(lambda: tr("Target Branch"), lambda: tr("SELECT"), callback=self._on_select_branch)
    self._branch_btn.set_visible(not ui_state.params.get_bool("IsTestedBranch"))
    self._branch_btn.action_item.set_value(ui_state.params.get("UpdaterTargetBranch") or "")
    self._branch_dialog: MultiOptionDialog | None = None

    # ---- 萝卜 UI 自定义参数：在「软件」菜单内分类放置 -------------------
    # 这 6 个参数已写入 params_keys.h 白名单（params_pyx.so 已重编）并落盘，
    # 此处仅在「软件」面板提供可视化调节控件，使它们从「萝卜」面板移出。
    # 说明：原先放在这里的三个颜色参数（盲区监控护栏颜色 / 雷达前车框颜色 /
    # 视觉前车框颜色）已移至「萝卜 → 显示 → 路径显示」，避免与萝卜菜单重复，
    # 此处不再定义任何颜色控件，也不再需要 ParamStore/StepperAction。
    # 停车状态三参数同样已移到「萝卜 → 显示 → 路径显示」。

    self._scroller = Scroller([
      self._onroad_label,
      self._version_item,
      self._quick_start_toggle,
      self._disable_updates_toggle,
      self._download_btn,
      self._install_btn,
      self._branch_btn,
      self._uninstall_btn,
    ], line_separator=True, spacing=0)

  def show_event(self):
    super().show_event()
    self._scroller.show_event()

  def hide_event(self):
    super().hide_event()
    self._scroller.hide_event()
    # 切换出软件菜单（或关闭设置）：卸载按钮恢复默认隐藏，连点计数清零
    self._uninstall_btn.set_visible(False)
    self._version_item.reset_taps()

  def _render(self, rect):
    self._scroller.render(rect)

  def _update_state(self):
    # Show/hide onroad warning
    self._onroad_label.set_visible(ui_state.is_onroad())

    # 同步两个开关的显示状态（标记/参数文件可能被外部改动）
    self._quick_start_toggle.action_item.set_state(quick_start_enabled())
    self._disable_updates_toggle.action_item.set_state(disable_updates_enabled())

    # Update current version and release notes
    current_desc = ui_state.params.get("UpdaterCurrentDescription") or ""
    current_release_notes = (ui_state.params.get("UpdaterCurrentReleaseNotes") or b"").decode("utf-8", "replace")
    self._version_item.action_item.set_text(current_desc)
    self._version_item.set_description(current_release_notes)

    # Update download button visibility and state
    self._download_btn.set_visible(ui_state.is_offroad())

    updater_state = ui_state.params.get("UpdaterState") or "idle"
    failed_count = ui_state.params.get("UpdateFailedCount") or 0
    fetch_available = ui_state.params.get_bool("UpdaterFetchAvailable")
    update_available = ui_state.params.get_bool("UpdateAvailable")

    if updater_state != "idle":
      # Updater responded
      self._waiting_for_updater = False
      self._download_btn.action_item.set_enabled(False)
      # Use the mapping, with a fallback to the original state string
      display_text = STATE_TO_DISPLAY_TEXT.get(updater_state, updater_state)
      self._download_btn.action_item.set_value(display_text)
    else:
      if failed_count > 0:
        self._download_btn.action_item.set_value(tr("failed to check for update"))
        self._download_btn.action_item.set_text(tr("CHECK"))
      elif fetch_available:
        self._download_btn.action_item.set_value(tr("update available"))
        self._download_btn.action_item.set_text(tr("DOWNLOAD"))
      else:
        last_update = ui_state.params.get("LastUpdateTime")
        if last_update:
          formatted = time_ago(last_update)
          self._download_btn.action_item.set_value(tr("up to date, last checked {}").format(formatted))
        else:
          self._download_btn.action_item.set_value(tr("up to date, last checked never"))
        self._download_btn.action_item.set_text(tr("CHECK"))

      # If we've been waiting too long without a state change, reset state
      if self._waiting_for_updater and (time.monotonic() - self._waiting_start_ts > UPDATED_TIMEOUT):
        self._waiting_for_updater = False

      # Only enable if we're not waiting for updater to flip out of idle
      self._download_btn.action_item.set_enabled(not self._waiting_for_updater)

    # Update target branch button value
    current_branch = ui_state.params.get("UpdaterTargetBranch") or ""
    self._branch_btn.action_item.set_value(current_branch)

    # Update install button
    self._install_btn.set_visible(ui_state.is_offroad() and update_available)
    if update_available:
      new_desc = ui_state.params.get("UpdaterNewDescription") or ""
      new_release_notes = (ui_state.params.get("UpdaterNewReleaseNotes") or b"").decode("utf-8", "replace")
      self._install_btn.action_item.set_text(tr("INSTALL"))
      self._install_btn.action_item.set_value(new_desc)
      self._install_btn.set_description(new_release_notes)
      # Enable install button for testing (like Qt showEvent)
      self._install_btn.action_item.set_enabled(True)
    else:
      self._install_btn.set_visible(False)

  def _on_download_update(self):
    # Check if we should start checking or start downloading
    self._download_btn.action_item.set_enabled(False)
    if self._download_btn.action_item.text == tr("CHECK"):
      # Start checking for updates
      self._waiting_for_updater = True
      self._waiting_start_ts = time.monotonic()
      os.system("pkill -SIGUSR1 -f system.updated.updated")
    else:
      # Start downloading
      self._waiting_for_updater = True
      self._waiting_start_ts = time.monotonic()
      os.system("pkill -SIGHUP -f system.updated.updated")

  def _reveal_uninstall(self):
    # 连点「当前版本」标题达标：显示卸载按钮
    self._uninstall_btn.set_visible(True)

  def _on_quick_start_toggle(self, state: bool):
    if state:
      # 创建预编译标记：launch_chffrplus.sh 见到它且无需强制重建时跳过 build.py
      try:
        with open(PREBUILT_MARKER, "wb") as f:
          f.write(b"")
      except OSError:
        pass
      # 行车中不立即重启（避免行驶中失控风险）：标记已生效，下次开机即跳过编译
      if not ui_state.is_onroad():
        os.system("sudo -n reboot")
    else:
      # 删除标记，恢复开机编译
      try:
        os.remove(PREBUILT_MARKER)
      except OSError:
        pass

  def _on_disable_updates_toggle(self, state: bool):
    # 与 echo -en "1" > /data/params/d/DisableUpdates 等价（关闭时写 "0"），重启后生效
    try:
      with open(DISABLE_UPDATES_PARAM, "wb") as f:
        f.write(b"1" if state else b"0")
    except OSError:
      pass

  def _on_uninstall(self):
    def handle_uninstall_confirmation(result: DialogResult):
      if result == DialogResult.CONFIRM:
        ui_state.params.put_bool("DoUninstall", True)

    dialog = ConfirmDialog(tr("Are you sure you want to uninstall?"), tr("Uninstall"), callback=handle_uninstall_confirmation)
    gui_app.push_widget(dialog)

  def _on_install_update(self):
    # Trigger reboot to install update
    self._install_btn.action_item.set_enabled(False)
    ui_state.params.put_bool("DoReboot", True)

  def _on_select_branch(self):
    # Get available branches and order
    current_git_branch = ui_state.params.get("GitBranch") or ""
    branches_str = ui_state.params.get("UpdaterAvailableBranches") or ""
    branches = [b for b in branches_str.split(",") if b]

    for b in [current_git_branch, "devel-staging", "devel", "nightly", "nightly-dev", "master"]:
      if b in branches:
        branches.remove(b)
        branches.insert(0, b)

    current_target = ui_state.params.get("UpdaterTargetBranch") or ""

    def handle_selection(result: DialogResult):
      # Confirmed selection
      if result == DialogResult.CONFIRM and self._branch_dialog is not None and self._branch_dialog.selection:
        selection = self._branch_dialog.selection
        ui_state.params.put("UpdaterTargetBranch", selection)
        self._branch_btn.action_item.set_value(selection)
        os.system("pkill -SIGUSR1 -f system.updated.updated")
      self._branch_dialog = None

    self._branch_dialog = MultiOptionDialog(tr("Select a branch"), branches, current_target, callback=handle_selection)
    gui_app.push_widget(self._branch_dialog)
