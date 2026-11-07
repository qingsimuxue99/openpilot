"""「垃圾清理」设置面板 —— 定期/手动扫描、清单、选择性删除、保护规则。

挂载在设置侧边栏「eGPU」下方。所有扫描与删除都在后台线程执行，
不阻塞 raylib 渲染主线程；删除默认移入隔离区，可一键恢复。

安全约定（与 cleanup_scanner.py 同源）：
  · 扫描阶段就把受保护文件排除，界面清单里根本不会出现它们；
  · 删除只接受「本次扫描清单里且被勾选」的路径；
  · 行驶中禁止删除；保护自检未通过时禁止删除；
  · 全部删除与彻底清空隔离区都要两重确认。
"""

import threading
import time

from openpilot.selfdrive.ui.layouts.settings import cleanup_scanner as cs
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import DialogResult, Widget
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog, alert_dialog
from openpilot.system.ui.widgets.list_view import (ListItem, button_item, dual_button_item,
                                                   multiple_button_item, simple_item, text_item)
from openpilot.system.ui.widgets.scroller_tici import Scroller

PAGE_SIZE = 12
INTERVAL_LABELS = ("关闭", "6 小时", "12 小时", "24 小时")
INTERVAL_HOURS = (0, 6, 12, 24)
AUTO_CHECK_SECONDS = 30.0


def short_path(path: str, limit: int = 26) -> str:
  if len(path) <= limit:
    return path
  return "..." + path[-(limit - 3):]


class CleanupLayout(Widget):
  """「垃圾清理」面板：状态 / 扫描 / 分类 / 文件清单 / 保护规则。"""

  def __init__(self):
    super().__init__()
    self._lock = threading.Lock()
    self._settings = cs.load_settings()
    self._result = cs.load_scan()
    self._selected: set[str] = set()
    self._page = 0
    self._busy_scan = False
    self._busy_action = False
    self._message = "尚未扫描 · 点「立即扫描」生成清单"
    self._trash = cs.trash_stats()

    items = self._build_items()
    self._scroller = Scroller(items, line_separator=True, spacing=0)

    # 常驻后台线程：即使没打开这个菜单，也会按设定周期在 offroad 时扫描。
    threading.Thread(target=self._auto_loop, name="cleanup-auto-scan", daemon=True).start()

  # -------------------------------------------------------------------------
  # 数据读取（每帧被列表项调用，必须轻量）
  # -------------------------------------------------------------------------
  def _items(self) -> list[dict]:
    return self._result.get("items", []) if self._result else []

  def _groups(self) -> list[dict]:
    if self._result and "groups" in self._result:
      return self._result["groups"]
    return cs.build_groups([])

  def _page_count(self) -> int:
    return max(1, (len(self._items()) + PAGE_SIZE - 1) // PAGE_SIZE)

  def _page_items(self) -> list[dict]:
    start = self._page * PAGE_SIZE
    return self._items()[start:start + PAGE_SIZE]

  def _page_item(self, index: int) -> dict | None:
    page = self._page_items()
    return page[index] if index < len(page) else None

  def _item_map(self) -> dict:
    return {it["path"]: it for it in self._items()}

  def _selected_items(self) -> list[dict]:
    picked = self._selected
    return [it for it in self._items() if it["path"] in picked]

  @staticmethod
  def _onroad() -> bool:
    try:
      return bool(getattr(ui_state, "started", False))
    except Exception:
      return False

  def _selfcheck_ok(self) -> bool:
    return bool((self._result or {}).get("selfcheck", {}).get("ok"))

  def _can_edit(self) -> bool:
    return (not self._busy_scan and not self._busy_action and not self._onroad()
            and self._result is not None and self._selfcheck_ok() and len(self._items()) > 0)

  # -------------------------------------------------------------------------
  # 后台任务
  # -------------------------------------------------------------------------
  def _spawn(self, name: str, fn):
    threading.Thread(target=fn, name=name, daemon=True).start()

  def _scan_async(self, manual: bool = False):
    with self._lock:
      if self._busy_scan or self._busy_action:
        return
      self._busy_scan = True
      if manual:
        self._message = "正在扫描…（只读，不删除任何文件）"

    def run():
      try:
        result = cs.scan()
        report = cs.save_scan(result)
        with self._lock:
          self._result = result
          self._selected.clear()
          self._page = 0
          self._settings = cs.load_settings()
          tag = "手动" if manual else "定期"
          self._message = ("%s扫描完成 · %d 项 · %s" %
                           (tag, len(result["items"]), cs.fmt_size(sum(it["size"] for it in result["items"]))))
      except Exception as exc:
        with self._lock:
          self._message = "扫描失败：" + type(exc).__name__
      finally:
        with self._lock:
          self._busy_scan = False

    self._spawn("cleanup-scan", run)

  def _auto_loop(self):
    """定期扫描：每 30 秒检查一次，仅在 offroad 且到达周期时扫描。"""
    while True:
      time.sleep(AUTO_CHECK_SECONDS)
      try:
        settings = cs.load_settings()
        self._settings = settings
        if not settings["auto"] or self._onroad():
          continue
        elapsed = time.time() - float(settings["last_scan"] or 0)
        if elapsed < settings["interval_hours"] * 3600:
          continue
        self._scan_async(manual=False)
      except Exception as exc:
        with self._lock:
          self._message = "定期扫描异常：" + type(exc).__name__

  def _run_trash(self, paths: list[str]):
    with self._lock:
      if self._busy_action:
        return
      self._busy_action = True
      self._message = "正在把 %d 项移入隔离区…" % len(paths)

    def run():
      try:
        out = cs.trash_items(paths)
        result = cs.scan()
        cs.save_scan(result)
        self._trash = cs.trash_stats()
        with self._lock:
          self._result = result
          self._selected.clear()
          self._page = 0
          tail = " · " + out["aborted"] if out["aborted"] else ""
          self._message = ("已隔离 %d 项 · 跳过 %d · 失败 %d%s · 清单已刷新" %
                           (len(out["moved"]), len(out["skipped"]), len(out["failed"]), tail))
      except Exception as exc:
        with self._lock:
          self._message = "隔离失败：" + type(exc).__name__
      finally:
        with self._lock:
          self._busy_action = False

    self._spawn("cleanup-trash", run)

  def _run_restore(self):
    def run():
      try:
        out = cs.restore_trash()
        self._trash = cs.trash_stats()
        with self._lock:
          self._message = "已恢复 %d 项 · 跳过 %d 项" % (len(out["restored"]), len(out["skipped"]))
      except Exception as exc:
        with self._lock:
          self._message = "恢复失败：" + type(exc).__name__

    self._spawn("cleanup-restore", run)

  def _run_purge(self):
    def run():
      try:
        out = cs.purge_trash()
        self._trash = cs.trash_stats()
        with self._lock:
          self._message = "隔离区已彻底删除 %d 个文件 · 失败 %d" % (len(out["removed"]), len(out["failed"]))
      except Exception as exc:
        with self._lock:
          self._message = "清空失败：" + type(exc).__name__

    self._spawn("cleanup-purge", run)

  # -------------------------------------------------------------------------
  # 按钮回调
  # -------------------------------------------------------------------------
  def _on_scan(self):
    self._scan_async(manual=True)

  def _on_interval(self, index: int):
    hours = INTERVAL_HOURS[index] if 0 <= index < len(INTERVAL_HOURS) else 0
    self._settings = cs.save_settings({"auto": hours > 0, "interval_hours": hours if hours else 6})
    self._message = "定期扫描已设为：" + INTERVAL_LABELS[index] + "（行驶中不扫描）"

  def _toggle_select(self, index: int):
    if not self._can_edit():
      return
    it = self._page_item(index)
    if not it:
      return
    with self._lock:
      if it["path"] in self._selected:
        self._selected.discard(it["path"])
      else:
        self._selected.add(it["path"])

  def _toggle_category(self, cat: str):
    if not self._can_edit():
      return
    paths = [it["path"] for it in self._items() if it["category"] == cat]
    with self._lock:
      if paths and all(p in self._selected for p in paths):
        self._selected.difference_update(paths)
      else:
        self._selected.update(paths)

  def _on_prev(self):
    with self._lock:
      self._page = max(0, self._page - 1)

  def _on_next(self):
    with self._lock:
      self._page = min(self._page_count() - 1, self._page + 1)

  def _blocked(self, why: str):
    gui_app.push_widget(alert_dialog(why))

  def _on_delete_selected(self):
    self._confirm_trash([it["path"] for it in self._selected_items()], "删除选中")

  def _on_delete_all(self):
    self._confirm_trash([it["path"] for it in self._items()], "全部删除")

  def _confirm_trash(self, paths: list[str], label: str):
    if not self._can_edit():
      self._blocked("当前不可删除：行驶中 / 保护自检未通过 / 尚无清单")
      return
    if not paths:
      self._blocked("请先勾选文件，或用「全部删除」")
      return
    size = sum(self._item_map().get(p, {}).get("size", 0) for p in paths)

    def second(result: DialogResult):
      if result == DialogResult.CONFIRM:
        self._run_trash(paths)

    def first(result: DialogResult):
      if result != DialogResult.CONFIRM:
        return
      gui_app.push_widget(ConfirmDialog(
        "最后确认：%s %d 项 / %s。移入隔离区后仍可恢复，不会立刻释放空间。" %
        (label, len(paths), cs.fmt_size(size)), "确认隔离", callback=second))

    gui_app.push_widget(ConfirmDialog(
      "%s：%d 项 / %s。这些文件会从原路径移到 /data/carrot_trash/，不会直接删除。" %
      (label, len(paths), cs.fmt_size(size)), "继续", callback=first))

  def _on_export(self):
    if not self._result:
      self._blocked("请先扫描再导出清单")
      return
    path = cs.save_scan(self._result)
    self._message = "完整清单已导出（路径见按钮说明）"

  def _on_restore(self):
    if not self._trash.get("count"):
      self._blocked("隔离区是空的")
      return

    def confirm(result: DialogResult):
      if result == DialogResult.CONFIRM:
        self._run_restore()

    gui_app.push_widget(ConfirmDialog(
      "把隔离区 %d 个文件按原路径放回？原路径已有文件会跳过，不覆盖。" % self._trash["count"],
      "恢复", callback=confirm))

  def _on_purge(self):
    if not self._trash.get("count"):
      self._blocked("隔离区是空的")
      return

    def second(result: DialogResult):
      if result == DialogResult.CONFIRM:
        self._run_purge()

    def first(result: DialogResult):
      if result != DialogResult.CONFIRM:
        return
      gui_app.push_widget(ConfirmDialog(
        "最后确认：彻底删除 %d 个文件，不可恢复！" % self._trash["count"],
        "彻底删除", callback=second))

    gui_app.push_widget(ConfirmDialog(
      "隔离区共 %d 个文件 / %s。彻底清空后无法恢复。" %
      (self._trash["count"], cs.fmt_size(self._trash.get("bytes", 0))),
      "继续", callback=first))

  # -------------------------------------------------------------------------
  # 显示文案
  # -------------------------------------------------------------------------
  def _status_text(self) -> str:
    if self._busy_scan:
      return "扫描中…"
    if self._busy_action:
      return "处理中…"
    if not self._result:
      return "尚无扫描结果"
    if not self._selfcheck_ok():
      return "保护自检未通过 · 已禁止删除"
    return "就绪 · %d 项可清理" % len(self._items())

  def _last_text(self) -> str:
    stamp = self._result.get("stamp") if self._result else None
    return stamp[5:16] if stamp and len(stamp) >= 16 else (stamp or "—")

  def _message_short(self) -> str:
    return self._message if len(self._message) <= 22 else self._message[:22] + ".."

  def _auto_text(self) -> str:
    st = self._settings
    if not st.get("auto"):
      return "关闭"
    hours = st.get("interval_hours", 6)
    nxt = float(st.get("last_scan") or 0) + hours * 3600
    tail = time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt > time.time() else "待扫描"
    return "%d 小时 · 下次 %s" % (hours, tail)

  def _total_text(self) -> str:
    if not self._result:
      return "—"
    total = sum(it["size"] for it in self._items())
    return "%d 项 · %s" % (len(self._items()), cs.fmt_size(total))

  def _selected_text(self) -> str:
    picked = self._selected_items()
    return "%d 项 · %s" % (len(picked), cs.fmt_size(sum(it["size"] for it in picked)))

  def _selfcheck_text(self) -> str:
    if not self._result:
      return "未扫描"
    sc = self._result.get("selfcheck") or {}
    keep = int(sc.get("keep_total") or 0) - len(sc.get("missing_keep") or [])
    ok = int(sc.get("protected_total") or 0) - len(sc.get("protected_fail") or [])
    return "通过 · 保护 %d/%d" % (ok, int(sc.get("protected_total") or 0)) if sc.get("ok") else "未通过"

  def _trash_text(self) -> str:
    return "%d 个 · %s" % (self._trash.get("count", 0), cs.fmt_size(self._trash.get("bytes", 0)))

  def _page_text(self) -> str:
    return "第 %d/%d 页 · 共 %d 项" % (self._page + 1, self._page_count(), len(self._items()))

  def _group_title(self, cat: str) -> str:
    g = next((x for x in self._groups() if x["category"] == cat), None)
    if not g:
      return cat
    return "%s · %d 项 · %s" % (g["name"], g["count"], cs.fmt_size(g["bytes"]))

  def _group_button(self, cat: str) -> str:
    paths = [it["path"] for it in self._items() if it["category"] == cat]
    if paths and all(p in self._selected for p in paths):
      return "取消全选"
    return "全选该类"

  def _group_desc(self, cat: str) -> str:
    g = next((x for x in self._groups() if x["category"] == cat), None)
    if not g:
      return ""
    return "<br>".join([
      "扫描路径：" + "、".join(g["paths"]),
      "作用：" + g["role"],
      "删除影响：" + g["effect"],
      "风险：" + g["risk"],
      "保留规则：" + g["rule"],
      "本次命中目录：" + ("、".join(g["dirs"]) if g["dirs"] else "无"),
    ])

  def _file_title(self, index: int) -> str:
    it = self._page_item(index)
    if not it:
      return ""
    return "%s  %s" % (short_path(it["path"]), cs.fmt_size(it["size"]))

  def _file_button(self, index: int) -> str:
    it = self._page_item(index)
    return "已选" if it and it["path"] in self._selected else "选中"

  def _file_desc(self, index: int) -> str:
    it = self._page_item(index)
    if not it:
      return ""
    return "<br>".join([
      "完整路径：" + it["path"],
      "类别：" + it["name"] + "（" + it["category"] + "）",
      "大小：" + cs.fmt_size(it["size"]) + " · 修改时间：" + cs.fmt_time(it["mtime"]),
      "作用：" + it["role"],
      "删除影响：" + it["effect"],
      "风险：" + it["risk"],
      "保留规则：" + it["rule"],
      "点右侧按钮切换选中；删除时只会移入隔离区，可恢复。",
    ])

  # -------------------------------------------------------------------------
  # 构建列表行（Scroller 不支持运行中删行，固定预创建 + set_visible）
  # -------------------------------------------------------------------------
  def _build_items(self) -> list:
    items = [
      text_item(lambda: tr("扫描状态"), self._status_text,
                lambda: tr("扫描只读，不删除任何文件；受保护文件在扫描阶段就被排除。")),
      text_item(lambda: tr("上次扫描"), self._last_text),
      text_item(lambda: tr("定期扫描"), self._auto_text,
                lambda: tr("定期扫描只在设备未行驶时执行；手动扫描随时可用。")),
      text_item(lambda: tr("候选合计"), self._total_text,
                lambda: "扫描阶段排除的受保护项：%d 项；完整清单见导出按钮。" %
                        int((self._result or {}).get("protected_hits") or 0)),
      text_item(lambda: tr("已选中"), self._selected_text),
      text_item(lambda: tr("保护自检"), self._selfcheck_text,
                lambda: tr("每次扫描都会验证：保护样例必须全部拦住、可删样例必须全部放行、关键文件必须在线。未通过时删除按钮不可用。")),
      text_item(lambda: tr("隔离区"), self._trash_text,
                lambda: tr("删除默认移入 /data/carrot_trash/，不直接删除；恢复或彻底清空都要单独操作。")),
      text_item(lambda: tr("最近操作"), self._message_short, lambda: self._message),
      button_item(lambda: tr("立即扫描"), lambda: tr("扫描"),
                  lambda: tr("只读扫描 7 类确定形态的文件，生成清单并刷新完整清单文件。"),
                  callback=self._on_scan, enabled=lambda: not self._busy_scan and not self._busy_action),
      multiple_button_item(lambda: tr("定期扫描周期"),
                           lambda: tr("关闭 / 6 小时 / 12 小时 / 24 小时。行驶中不执行定期扫描。"),
                           list(INTERVAL_LABELS), self._interval_index(),
                           callback=self._on_interval),
      dual_button_item(lambda: tr("删除选中（隔离）"), lambda: tr("全部删除（隔离）"),
                       left_callback=self._on_delete_selected, right_callback=self._on_delete_all),
      button_item(lambda: tr("导出完整清单"), lambda: tr("导出"),
                  lambda: tr("把完整清单（路径 / 大小 / 作用 / 风险 / 保护规则）写到 /data/carrot_state/cleanup_report.txt，可用 SSH 或网页查看。"),
                  callback=self._on_export),
      button_item(lambda: tr("恢复隔离区"), lambda: tr("恢复"),
                  lambda: tr("把隔离区文件按原路径放回；原路径已有文件时跳过，不覆盖。"),
                  callback=self._on_restore, enabled=lambda: not self._busy_action),
      button_item(lambda: tr("彻底清空隔离区"), lambda: tr("清空"),
                  lambda: tr("危险：彻底删除隔离区文件，不可恢复。需要两重确认。"),
                  callback=self._on_purge, enabled=lambda: not self._busy_action),
      simple_item(lambda: tr("===== 可删类别（点击展开作用 / 风险 / 规则）=====")),
    ]

    for cat in cs.CATEGORY_ORDER:
      row = button_item(lambda c=cat: self._group_title(c),
                        lambda c=cat: self._group_button(c),
                        lambda c=cat: self._group_desc(c),
                        callback=lambda c=cat: self._toggle_category(c),
                        enabled=lambda: self._can_edit())
      items.append(row)

    items.append(simple_item(lambda: tr("===== 文件清单（点行展开完整路径与作用）=====")))
    items.append(text_item(lambda: tr("清单页码"), self._page_text))
    items.append(dual_button_item(lambda: tr("上一页"), lambda: tr("下一页"),
                                  left_callback=self._on_prev, right_callback=self._on_next))

    self._file_rows: list[ListItem] = []
    for i in range(PAGE_SIZE):
      row = button_item(lambda i=i: self._file_title(i),
                        lambda i=i: self._file_button(i),
                        lambda i=i: self._file_desc(i),
                        callback=lambda i=i: self._toggle_select(i),
                        enabled=lambda: self._can_edit())
      row.set_visible(lambda i=i: i < len(self._page_items()))
      self._file_rows.append(row)
      items.append(row)

    items.append(simple_item(lambda: tr("===== 保护规则（点击每条展开）=====")))
    for title, body in cs.rule_sections():
      items.append(ListItem(title=lambda t=title: t, description=lambda b=body: b))

    return items

  def _interval_index(self) -> int:
    hours = self._settings.get("interval_hours", 6) if self._settings.get("auto") else 0
    return INTERVAL_HOURS.index(hours) if hours in INTERVAL_HOURS else 0

  # -------------------------------------------------------------------------
  # 生命周期
  # -------------------------------------------------------------------------
  def show_event(self):
    super().show_event()
    self._settings = cs.load_settings()
    self._result = cs.load_scan() or self._result
    self._trash = cs.trash_stats()
    self._scroller.show_event()

  def hide_event(self):
    super().hide_event()
    self._scroller.hide_event()

  def _render(self, rect):
    self._scroller.render(rect)
