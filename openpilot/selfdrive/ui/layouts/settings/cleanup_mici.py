"""mici 小屏设置页的「垃圾清理」面板。

与主设置页共用 cleanup_scanner.py 的同一份保护规则与删除逻辑（不会误删），
只是控件换成 mici 的 BigButton / 滑动确认。这里按「类别」做选择性删除，
逐文件清单与完整保护规则导出到 /data/carrot_state/cleanup_report.txt。
"""

import threading
import time

from openpilot.selfdrive.ui.layouts.settings import cleanup_scanner as cs
from openpilot.selfdrive.ui.mici.widgets.button import BigButton
from openpilot.selfdrive.ui.mici.widgets.dialog import BigConfirmationDialog
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.widgets.scroller import NavScroller

INTERVAL_LABELS = ("关闭", "6 小时", "12 小时", "24 小时")
INTERVAL_HOURS = (0, 6, 12, 24)
AUTO_CHECK_SECONDS = 30.0
UI_REFRESH_SECONDS = 0.5


class CleanupLayoutMici(NavScroller):
  def __init__(self):
    super().__init__()
    self._lock = threading.Lock()
    self._settings = cs.load_settings()
    self._result = cs.load_scan()
    self._selected_cats: set[str] = set()
    self._busy = False
    self._message = "尚未扫描"
    self._trash = cs.trash_stats()
    self._purge_armed = False
    self._purge_armed_at = 0.0
    self._last_ui = 0.0

    self._icon = gui_app.texture("icons_mici/settings/device_icon.png", 72, 58)
    self._status = BigButton("清理状态", "只读扫描", self._icon)
    self._scan = BigButton("立即扫描", "只读，不删除", self._icon)
    self._auto = BigButton("定期扫描", "关闭", self._icon)
    self._summary = BigButton("候选合计", "0 项", self._icon)
    self._selfcheck = BigButton("保护自检", "未扫描", self._icon)
    self._trash_btn = BigButton("隔离区", "0 个", self._icon)
    self._picked = BigButton("已选类别", "0 类", self._icon)
    self._delete_sel = BigButton("删除选中类别", "移到隔离区", self._icon)
    self._delete_all = BigButton("全部删除", "移到隔离区", self._icon)
    self._export = BigButton("导出完整清单", "含保护规则", self._icon)
    self._restore = BigButton("恢复隔离区", "按原路径放回", self._icon)
    self._purge = BigButton("彻底清空隔离区", "不可恢复", self._icon)
    self._message_btn = BigButton("最近操作", self._message, self._icon)
    self._rules = BigButton("保护规则", "9+9 项永不扫描", self._icon)

    self._scan.set_click_callback(self._on_scan)
    self._auto.set_click_callback(self._on_auto)
    self._delete_sel.set_click_callback(self._on_delete_selected)
    self._delete_all.set_click_callback(self._on_delete_all)
    self._export.set_click_callback(self._on_export)
    self._restore.set_click_callback(self._on_restore)
    self._purge.set_click_callback(self._on_purge)
    self._rules.set_click_callback(self._on_rules)

    self._cat_buttons = {}
    for cat in cs.CATEGORY_ORDER:
      btn = BigButton(cs.CATEGORIES[cat]["name"], "0 项", self._icon)
      btn.set_click_callback(lambda c=cat: self._toggle_cat(c))
      self._cat_buttons[cat] = btn

    widgets = [
      self._status, self._scan, self._auto, self._summary, self._selfcheck,
      self._trash_btn, self._picked, self._delete_sel, self._delete_all,
      self._export, self._restore, self._purge, self._rules, self._message_btn,
    ] + [self._cat_buttons[c] for c in cs.CATEGORY_ORDER]
    self._scroller.add_widgets(widgets)

    threading.Thread(target=self._auto_loop, name="cleanup-mici-auto", daemon=True).start()

  # -------------------------------------------------------------------------
  def _items(self) -> list[dict]:
    return self._result.get("items", []) if self._result else []

  @staticmethod
  def _onroad() -> bool:
    try:
      return bool(getattr(ui_state, "started", False))
    except Exception:
      return False

  def _can_edit(self) -> bool:
    return (not self._busy and not self._onroad() and self._result is not None
            and bool((self._result or {}).get("selfcheck", {}).get("ok")) and len(self._items()) > 0)

  def _selected_paths(self) -> list[str]:
    return [it["path"] for it in self._items() if it["category"] in self._selected_cats]

  def _spawn(self, name: str, fn):
    threading.Thread(target=fn, name=name, daemon=True).start()

  def _scan_async(self, manual: bool = False):
    with self._lock:
      if self._busy:
        return
      self._busy = True
      self._message = "正在扫描…"

    def run():
      try:
        result = cs.scan()
        cs.save_scan(result)
        with self._lock:
          self._result = result
          self._settings = cs.load_settings()
          self._message = "%s扫描完成 · %d 项" % ("手动" if manual else "定期", len(result["items"]))
      except Exception as exc:
        with self._lock:
          self._message = "扫描失败：" + type(exc).__name__
      finally:
        with self._lock:
          self._busy = False

    self._spawn("cleanup-mici-scan", run)

  def _auto_loop(self):
    while True:
      time.sleep(AUTO_CHECK_SECONDS)
      try:
        settings = cs.load_settings()
        self._settings = settings
        if not settings["auto"] or self._onroad():
          continue
        if time.time() - float(settings["last_scan"] or 0) < settings["interval_hours"] * 3600:
          continue
        self._scan_async(manual=False)
      except Exception as exc:
        with self._lock:
          self._message = "定期扫描异常：" + type(exc).__name__

  def _run_trash(self, paths):
    with self._lock:
      if self._busy:
        return
      self._busy = True
      self._message = "正在移入隔离区…"

    def run():
      try:
        out = cs.trash_items(paths)
        result = cs.scan()
        cs.save_scan(result)
        self._trash = cs.trash_stats()
        with self._lock:
          self._result = result
          self._selected_cats.clear()
          self._message = "已隔离 %d 项 · 跳过 %d · 失败 %d" % (
            len(out["moved"]), len(out["skipped"]), len(out["failed"]))
      except Exception as exc:
        with self._lock:
          self._message = "隔离失败：" + type(exc).__name__
      finally:
        with self._lock:
          self._busy = False

    self._spawn("cleanup-mici-trash", run)

  # -------------------------------------------------------------------------
  def _on_scan(self):
    self._scan_async(manual=True)

  def _on_auto(self):
    cur = self._settings.get("interval_hours", 6) if self._settings.get("auto") else 0
    idx = INTERVAL_HOURS.index(cur) if cur in INTERVAL_HOURS else 0
    idx = (idx + 1) % len(INTERVAL_HOURS)
    hours = INTERVAL_HOURS[idx]
    self._settings = cs.save_settings({"auto": hours > 0, "interval_hours": hours if hours else 6})
    self._message = "定期扫描：" + INTERVAL_LABELS[idx]

  def _toggle_cat(self, cat: str):
    if not self._can_edit():
      return
    with self._lock:
      if cat in self._selected_cats:
        self._selected_cats.discard(cat)
      else:
        self._selected_cats.add(cat)

  def _confirm_trash(self, paths, label: str):
    if not self._can_edit():
      self._message = "当前不可删除（行驶中 / 自检未通过 / 无清单）"
      return
    if not paths:
      self._message = "请先选择类别，或用「全部删除」"
      return

    def do():
      self._run_trash(paths)

    def second():
      gui_app.push_widget(BigConfirmationDialog(
        "滑动二次确认\n" + label + " " + str(len(paths)) + " 项", self._icon, do, red=True))

    gui_app.push_widget(BigConfirmationDialog(
      "滑动确认\n" + label + " " + str(len(paths)) + " 项（移到隔离区，可恢复）",
      self._icon, second, red=True))

  def _on_delete_selected(self):
    self._confirm_trash(self._selected_paths(), "删除选中类别")

  def _on_delete_all(self):
    self._confirm_trash([it["path"] for it in self._items()], "全部删除")

  def _on_export(self):
    if not self._result:
      self._message = "请先扫描"
      return
    path = cs.save_scan(self._result)
    self._message = "完整清单与保护规则：" + path

  def _on_rules(self):
    if not self._result:
      self._message = "请先扫描"
      return
    path = cs.save_scan(self._result)
    self._message = "保护名单 9+9 项、7 类可删，全文：" + path

  def _on_restore(self):
    if not self._trash.get("count"):
      self._message = "隔离区是空的"
      return

    def do():
      def run():
        out = cs.restore_trash()
        self._trash = cs.trash_stats()
        with self._lock:
          self._message = "已恢复 %d 项 · 跳过 %d" % (len(out["restored"]), len(out["skipped"]))
      self._spawn("cleanup-mici-restore", run)

    gui_app.push_widget(BigConfirmationDialog("滑动确认\n恢复隔离区文件", self._icon, do))

  def _on_purge(self):
    if not self._trash.get("count"):
      self._message = "隔离区是空的"
      return
    now = time.monotonic()
    if not self._purge_armed or now - self._purge_armed_at > 10.0:
      self._purge_armed = True
      self._purge_armed_at = now
      self._message = "10 秒内再次点击「彻底清空隔离区」才执行"
      return
    self._purge_armed = False

    def do():
      def run():
        out = cs.purge_trash()
        self._trash = cs.trash_stats()
        with self._lock:
          self._message = "隔离区已彻底删除 %d 个文件" % len(out["removed"])
      self._spawn("cleanup-mici-purge", run)

    gui_app.push_widget(BigConfirmationDialog("滑动确认\n彻底删除，不可恢复", self._icon, do, red=True))

  # -------------------------------------------------------------------------
  def _message_short(self) -> str:
    return self._message if len(self._message) <= 20 else self._message[:20] + ".."

  def _refresh_ui(self):
    items = self._items()
    sc = (self._result or {}).get("selfcheck") or {}
    self._status.set_value("扫描中…" if self._busy else ("%d 项" % len(items)))
    self._auto.set_value(INTERVAL_LABELS[self._interval_index()])
    self._summary.set_value("%d 项" % len(items))
    self._selfcheck.set_value("通过" if sc.get("ok") else ("未通过" if sc else "未扫描"))
    self._trash_btn.set_value("%d 个" % self._trash.get("count", 0))
    self._picked.set_value("%d 类" % len(self._selected_cats))
    self._message_btn.set_value(self._message_short())
    self._purge.set_value("再次点击确认" if self._purge_armed else "不可恢复")
    for cat, btn in self._cat_buttons.items():
      subset = [it for it in items if it["category"] == cat]
      mark = " · 已选" if cat in self._selected_cats else ""
      btn.set_value("%d 项%s" % (len(subset), mark))

  def _interval_index(self) -> int:
    hours = self._settings.get("interval_hours", 6) if self._settings.get("auto") else 0
    return INTERVAL_HOURS.index(hours) if hours in INTERVAL_HOURS else 0

  def _update_state(self):
    super()._update_state()
    now = time.monotonic()
    if now - self._last_ui < UI_REFRESH_SECONDS:
      return
    self._last_ui = now
    if self._purge_armed and now - self._purge_armed_at > 10.0:
      self._purge_armed = False
    self._refresh_ui()

  def show_event(self):
    super().show_event()
    self._settings = cs.load_settings()
    self._result = cs.load_scan() or self._result
    self._trash = cs.trash_stats()
    self._refresh_ui()
