"""模型（eGPU 大模型库）设置面板 —— 在设备屏幕上直接选模型、下载、看进度。

数据源与网页模型库完全同源：本机 carrot_server 的 ``127.0.0.1:7000`` 接口。
模型时间取自 commaai/openpilot 上游提交记录（model_id 里嵌着 commit / PR 号），
平时只用随代码携带的历史快照，只有大佬模型库出现新模型时才现拉一次上游历史。
所有 HTTP 请求都在后台线程执行，绝不阻塞 raylib 的渲染主线程；面板不可见时
不轮询。这里只做「选择 / 下载 / 进度 / 重启生效」，删除模型等破坏性操作仍留在网页端。

不对网页服务做任何假设：服务没起来时面板显示可读的提示，不抛异常、不空白。
"""

import json
import os
import re
import threading
import time
import urllib.request
from collections.abc import Callable

import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import (gui_app, TextAlignment,
                                                 TextAlignmentVertical)
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets import DialogResult, Widget
from openpilot.system.ui.widgets.confirm_dialog import ConfirmDialog
from openpilot.system.ui.widgets.label import gui_label
from openpilot.system.ui.widgets.list_view import (ITEM_TEXT_VALUE_COLOR, ItemAction, ListItem,
                                                   button_item, simple_item, text_item)
from openpilot.system.ui.widgets.scroller_tici import Scroller

BASE_URL = "http://127.0.0.1:7000"
MODELS_URL = BASE_URL + "/api/egpu/models"
STATUS_URL = BASE_URL + "/api/egpu/model"
SELECT_URL = BASE_URL + "/api/egpu/models/select"

# 上游 big_driving_tinygrad.pkl 的提交历史，用来给模型标注「上游提交日期」。
UPSTREAM_COMMITS_URL = (
  "https://api.github.com/repos/commaai/openpilot/commits"
  "?path=openpilot/selfdrive/modeld/models/big_driving_tinygrad.pkl&per_page=30"
)
UPSTREAM_TIMEOUT = 6.0

# 记录「上次见过的模型」清单，用来判断大佬模型库是否上新。
# 自建状态文件放 /data/carrot_state/（/data/params/d/ 会被 manager 开机清空）。
KNOWN_SHA_FILE = "/data/carrot_state/model_known_shas.json"

MAX_MODELS = 24           # 预创建的列表行上限，够覆盖远端模型库（当前 14 个）
REFRESH_SECONDS = 1.0    # 面板可见时的轮询间隔
HTTP_TIMEOUT = 4.0
BUSY_STATES = {"checking", "downloading", "verifying", "compiling", "installing", "waiting_for_network"}

STATE_CN = {
  "idle": "空闲",
  "checking": "检查中…",
  "downloading": "下载中…",
  "verifying": "校验中…",
  "compiling": "编译中…",
  "installing": "安装中…",
  "installed": "已安装",
  "compiled": "已就绪",
  "ready": "就绪（待点火）",
  "waiting_for_ignition": "等待点火",
  "waiting_for_network": "等待网络…",
  "error": "出错",
  "recovery_ready": "恢复就绪",
  "recovering": "恢复中…",
  "recovery_stopped": "恢复已停止",
}

# 2026-11-07 从 commaai/openpilot 提交记录实证得到的日期快照。前半是 master 上
# big_driving_tinygrad.pkl 的完整历史（17 条），后半是 model_id 引用、但未出现在
# 该路径历史里的上游提交。只作 GitHub 拉不到时的兜底，不参与任何编造。
KNOWN_COMMIT_DATES = {
  "b92fc482": "2026-11-05",  # Light-speed 2 (#39236)
  "ad4c9cdf": "2026-11-02",  # bump tinygrad usb wrap fix (#39204)
  "880b40e1": "2026-10-31",  # modeld tinygrad jit copies (#39187)
  "afedebcf": "2026-10-31",  # Revert (#39184)
  "0152aab4": "2026-10-29",  # bump tinygrad (#39176)
  "9fba666f": "2026-10-24",  # Flip Flop Model (#39156)
  "1e219d77": "2026-10-14",  # MD v3 (#39087)
  "21edbbcf": "2026-10-14",  # bump tinygrad (#39085)
  "6f4c0faf": "2026-10-10",  # Mountain Dew Model (#39047)
  "d05c2d97": "2026-10-09",  # bump tinygrad (#39054)
  "f59056e5": "2026-10-07",  # Revert Mountain Dew (#39046)
  "0e0c7c71": "2026-10-07",  # Mountain Dew Model (#39045)
  "4bcf7328": "2026-10-06",  # ResAction (#39037)
  "c6d13eb7": "2026-09-18",  # Cinque v3 (#38932)
  "df7e0e5e": "2026-09-18",  # upstream tinygrad disk tensors (#38956)
  "3cd1b6e4": "2026-09-18",  # retargetable model artifacts (#38933)
  "6080cc61": "2026-09-16",  # precompiled eGPU driving model (#38930)
  "c7cbe5de": "2026-10-29",  # Light Speed 重编译
  "afbf2e54": "2026-10-14",  # n16 nav-wm model
  "e46b35b0": "2026-10-17",  # Train Track checkpoint
  "37bfa141": "2026-09-08",  # Cinque v2
  "a2e422ee": "2026-09-01",  # TGC (#38739)
}

KNOWN_PR_DATES = {
  "39236": "2026-11-05", "39204": "2026-11-02", "39187": "2026-10-31",
  "39184": "2026-10-31", "39176": "2026-10-29", "39156": "2026-10-24",
  "39087": "2026-10-14", "39085": "2026-10-14", "39047": "2026-10-10",
  "39054": "2026-10-09", "39046": "2026-10-07", "39045": "2026-10-07",
  "39037": "2026-10-06", "38932": "2026-09-18", "38956": "2026-09-18",
  "38933": "2026-09-18", "38930": "2026-09-16", "38823": "2026-09-08",
  "38739": "2026-09-01",
}


def _get_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict:
  with urllib.request.urlopen(url, timeout=timeout) as resp:
    return json.loads(resp.read().decode("utf-8", "replace"))


def _post_json(url: str, payload: dict, timeout: float = 6.0) -> dict:
  req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json"}, method="POST")
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return json.loads(resp.read().decode("utf-8", "replace"))


def _load_known_shas() -> set[str]:
  """读回上次见过的模型 sha 集合；文件不存在或损坏都当空集合。"""
  try:
    with open(KNOWN_SHA_FILE, "r", encoding="utf-8") as f:
      data = json.load(f)
    return {str(x) for x in (data.get("shas") or []) if x}
  except Exception:
    return set()


def _save_known_shas(shas: set[str]) -> None:
  try:
    os.makedirs(os.path.dirname(KNOWN_SHA_FILE), exist_ok=True)
    tmp = KNOWN_SHA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump({"shas": sorted(shas)}, f)
    os.replace(tmp, KNOWN_SHA_FILE)
  except Exception:
    pass


def fetch_upstream_dates(timeout: float = UPSTREAM_TIMEOUT) -> tuple[dict, dict]:
  """现拉上游模型文件提交历史，返回 (8 位 commit 前缀 -> 日期, PR 号 -> 日期)。"""
  req = urllib.request.Request(UPSTREAM_COMMITS_URL, headers={
    "User-Agent": "carrot-model/1",
    "Accept": "application/vnd.github+json",
  })
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    items = json.loads(resp.read().decode("utf-8", "replace"))
  by8, bypr = {}, {}
  if not isinstance(items, list):
    return by8, bypr
  for item in items:
    if not isinstance(item, dict):
      continue
    commit = item.get("commit") or {}
    date = str((commit.get("author") or {}).get("date") or "")[:10]
    sha = str(item.get("sha") or "")
    if len(date) != 10 or len(sha) < 8:
      continue
    by8[sha[:8]] = date
    lines = str(commit.get("message") or "").splitlines()
    match = re.search(r"\(#(\d+)\)", lines[0]) if lines else None
    if match:
      bypr[match.group(1)] = date
  return by8, bypr


def resolve_model_date(entry: dict, by8: dict, bypr: dict) -> str | None:
  """从 model_id 里嵌的上游 commit / PR 号还原模型发布日期，查不到就返回 None。"""
  model_id = str(entry.get("model_id") or "")
  for token in re.findall(r"[0-9a-f]{8}", model_id):
    if token in by8:
      return by8[token]
    if token in KNOWN_COMMIT_DATES:
      return KNOWN_COMMIT_DATES[token]
  match = re.search(r"pr(\d+)", model_id)
  if match:
    return bypr.get(match.group(1)) or KNOWN_PR_DATES.get(match.group(1))
  return None


def short_date(date) -> str:
  """'2026-11-05' -> '11-05'，用于在较窄的值区显示。"""
  return date[5:] if isinstance(date, str) and len(date) == 10 else ""


def fmt_size(value) -> str:
  """字节数转 MB 文案；取不到时给中文占位，不显示 0 MB 误导。"""
  try:
    n = int(value or 0)
  except (TypeError, ValueError):
    return "大小未知"
  return f"{n / 1024 / 1024:.0f} MB" if n > 0 else "大小未知"


def state_cn(phase) -> str:
  return STATE_CN.get(phase or "", str(phase or "未知"))


def model_title(entry: dict, selected_sha) -> str:
  # 右侧按钮已用「已选中」标明当前模型；标题再加「（当前）」会挤占数值区宽度，
  # 使「已下载 · 732 MB · 11-05」被 gui_label 截成省略号。见 list_view.get_right_item_rect。
  return str(entry.get("label") or entry.get("model_id") or str(entry.get("sha256", ""))[:12])


def model_button_text(entry: dict, selected_sha) -> str:
  sha = entry.get("sha256")
  if selected_sha and sha and sha == selected_sha:
    return "已选中"
  return "使用" if entry.get("downloaded") else "下载"


def model_value_text(entry: dict, running_sha, date: str | None = None) -> str:
  # 双方都不能为空：缺 sha256 的条目与 running=None 会「None == None」误判成运行中
  sha = entry.get("sha256")
  if running_sha and sha and sha == running_sha:
    head = "运行中"
  else:
    head = "已下载" if entry.get("downloaded") else "未下载"
  size = fmt_size(entry.get("size")).replace(" ", "")
  tail = f" · {short_date(date)}" if date else " · 时间未知"
  return head + " · " + size + tail


def status_text(status: dict, selected_sha) -> str:
  """把状态接口的英文阶段翻成中文，并区分「旧模型的残留状态」。"""
  if not status:
    return "正在读取…"
  phase = status.get("state")
  text = state_cn(phase)
  sha = status.get("sha256")
  if sha and selected_sha and sha != selected_sha:
    text += "（上一个模型）" if phase not in BUSY_STATES else "（准备中）"
  if phase == "error" and status.get("detail"):
    text += "：" + str(status["detail"])
  return text


def progress_text(status: dict, selected_sha) -> str:
  if not status:
    return "—"
  sha = status.get("sha256")
  if sha and selected_sha and sha != selected_sha and status.get("state") in BUSY_STATES:
    return "准备中…"
  pct = status.get("progress")
  if pct is None:
    return "—"
  total = int(status.get("total_bytes") or 0)
  done = int(status.get("downloaded_bytes") or 0)
  tail = f" · {fmt_size(done)} / {fmt_size(total)}" if total > 0 else ""
  return f"{float(pct):.1f}%" + tail


class _ProgressAction(ItemAction):
  """列表右侧的图形进度条；纯绘制，不产生点击。

  进度条与文字分上下两层：旧版把两者挤在同一行，右侧文字区只剩约 220px，
  「100.0% · 732 MB / 732 MB」会被 gui_label 省略成「100.0% · 7...」。
  现在文字独占整行宽度，字号略缩，完整显示不再被截断。
  """

  WIDTH = 520

  def __init__(self, get_pct: Callable[[], float], get_text: Callable[[], str]):
    super().__init__(width=self.WIDTH)
    self._get_pct = get_pct
    self._get_text = get_text

  def get_width_hint(self) -> float:
    return self.WIDTH

  def _render(self, rect: rl.Rectangle):
    try:
      pct = max(0.0, min(100.0, float(self._get_pct() or 0.0)))
    except (TypeError, ValueError):
      pct = 0.0

    bar_w = max(60.0, rect.width - 12)
    bar_h = 26.0
    bar_x = rect.x + 4
    bar_y = rect.y + 32
    rl.draw_rectangle_rounded(rl.Rectangle(bar_x, bar_y, bar_w, bar_h), 0.5, 8, rl.Color(68, 68, 68, 255))
    if pct > 0:
      rl.draw_rectangle_rounded(
        rl.Rectangle(bar_x, bar_y, bar_w * pct / 100.0, bar_h), 0.5, 8, rl.Color(240, 150, 50, 255))

    txt_rect = rl.Rectangle(rect.x, rect.y + 66, max(60.0, rect.width - 8),
                            max(40.0, rect.height - 70))
    gui_label(txt_rect, self._get_text(), font_size=28, color=ITEM_TEXT_VALUE_COLOR,
              alignment=TextAlignment.RIGHT, alignment_vertical=TextAlignmentVertical.MIDDLE)
    return False


class ModelLayout(Widget):
  """「模型」面板：列出模型库、一键选择并下载、实时显示进度。"""

  def __init__(self):
    super().__init__()
    self._lock = threading.Lock()
    self._models: list[dict] = []
    self._dates: dict[str, str] = {}
    self._selected = None
    self._running = None
    self._usbgpu: dict = {}
    self._status: dict = {}
    self._remote_catalog = False
    self._error = ""
    self._pick_error = ""
    self._visible = False
    self._refreshing = False
    self._picking = False
    self._last_refresh = 0.0
    # 手动刷新的可见反馈：阶段、结果文案、完成时刻、时间戳
    self._refresh_phase = "idle"   # idle / busy / ok / fail
    self._refresh_msg = ""
    self._refresh_stamp = ""
    self._refresh_at = 0.0
    # 上游提交日期：平时只用随代码带的历史快照；仅当大佬模型库上新时才现拉一次
    self._upstream_by8 = dict(KNOWN_COMMIT_DATES)
    self._upstream_bypr = dict(KNOWN_PR_DATES)
    self._upstream_ok = False
    self._upstream_running = False
    self._known_shas = _load_known_shas()

    self._library_item = text_item(lambda: tr("模型库"), lambda: self._library_text())
    self._selected_item = text_item(lambda: tr("当前选择"), lambda: self._selected_text())
    self._state_item = text_item(lambda: tr("状态"), lambda: self._state_text())
    self._progress_item = ListItem(
      title=lambda: tr("下载进度"),
      action_item=_ProgressAction(lambda: self._progress_pct(), lambda: self._progress_text()),
    )
    self._run_item = text_item(lambda: tr("eGPU"), lambda: self._run_text())
    self._refresh_btn = button_item(lambda: tr("刷新模型库"),
                                    lambda: self._refresh_button_text(),
                                    callback=self._on_refresh)
    self._reboot_btn = button_item(
      lambda: tr("重启生效"), lambda: tr("重启"),
      lambda: tr("下载完成后重启设备以加载新模型。行车中不可使用。"),
      callback=self._on_reboot, enabled=lambda: self._can_restart(),
    )

    items = [
      self._library_item,
      self._selected_item,
      self._state_item,
      self._progress_item,
      self._run_item,
      self._refresh_btn,
      self._reboot_btn,
      simple_item(lambda: tr("按上游提交日期排序 · 点右侧按钮下载")),
    ]

    # 固定预创建列表行：Scroller 不支持运行中删行，用可见性控制数量，
    # 用 callable 让标题/按钮/数值每帧读取最新数据。
    self._rows: list[ListItem] = []
    for i in range(MAX_MODELS):
      row = button_item(
        lambda i=i: self._row_title(i),
        lambda i=i: self._row_button(i),
        lambda i=i: self._row_desc(i),
        callback=lambda i=i: self._on_pick(i),
        enabled=lambda i=i: self._row_enabled(i),
      )
      row.action_item.set_value(lambda i=i: self._row_value(i))
      row.set_visible(lambda i=i: i < len(self._models))
      self._rows.append(row)
      items.append(row)

    self._scroller = Scroller(items, line_separator=True, spacing=0)

  # ---- 后台数据 -----------------------------------------------------------
  def _build_entries(self, raw: list, by8: dict, bypr: dict):
    """按上游提交日期倒序整理模型列表，并算出 sha -> 日期。"""
    entries = list(raw)
    # 稳定排序：有日期的新→旧，没日期的保持原顺序排最后
    entries.sort(key=lambda e: resolve_model_date(e, by8, bypr) or "", reverse=True)
    entries = entries[:MAX_MODELS]
    dates = {str(e.get("sha256") or ""): d for e in entries
             if (d := resolve_model_date(e, by8, bypr))}
    return entries, dates

  def _maybe_sync_upstream(self, raw_models: list):
    """只有大佬模型库出现「上次没见过」的模型，才去 GitHub 补一次日期。"""
    shas = {str(e.get("sha256") or "") for e in raw_models if e.get("sha256")}
    if not shas:
      return
    with self._lock:
      known = set(self._known_shas)
      running = self._upstream_running
    if not known:
      # 首次运行只建立基线：老模型日期用随代码带的快照，不查 GitHub
      self._known_shas = shas
      _save_known_shas(shas)
      return
    if not (shas - known) or running:
      return
    # 先落新基线，避免 GitHub 不通时每次刷新都重复请求
    self._known_shas = shas
    _save_known_shas(shas)
    with self._lock:
      self._upstream_running = True
    self._sync_upstream_async()

  def _sync_upstream_async(self):
    """仅在大佬模型库上新时调用：现拉一次上游提交记录，不拖住列表显示。"""
    def run():
      try:
        fresh8, fresh_pr = fetch_upstream_dates()
        with self._lock:
          self._upstream_by8 = {**KNOWN_COMMIT_DATES, **fresh8}
          self._upstream_bypr = {**KNOWN_PR_DATES, **fresh_pr}
          self._upstream_ok = True
          if self._models:
            entries, dates = self._build_entries(self._models, self._upstream_by8, self._upstream_bypr)
            self._models = entries
            self._dates = dates
      except Exception:
        pass  # 保留历史快照，不报错、不阻塞
      finally:
        with self._lock:
          self._upstream_running = False

    threading.Thread(target=run, name="model-picker-upstream", daemon=True).start()

  def _refresh_async(self, force: bool = False, manual: bool = False):
    # 手动刷新必须立刻给界面一个「正在刷新」，哪怕后台已有一轮在跑。
    if manual:
      self._refresh_phase = "busy"
      self._refresh_msg = "正在刷新…"
    if self._refreshing:
      return
    now = time.monotonic()
    if not force and now - self._last_refresh < REFRESH_SECONDS:
      return
    self._last_refresh = now
    self._refreshing = True
    wants_result = manual

    def run():
      try:
        models = _get_json(MODELS_URL)
        status = _get_json(STATUS_URL)
        raw = models.get("models") or []
        self._maybe_sync_upstream(raw)
        with self._lock:
          by8, bypr = dict(self._upstream_by8), dict(self._upstream_bypr)
          entries, dates = self._build_entries(raw, by8, bypr)
          self._models = entries
          self._dates = dates
          self._selected = models.get("selected")
          self._running = models.get("running")
          self._usbgpu = models.get("usbgpu") or {}
          self._remote_catalog = bool(models.get("remote_catalog"))
          self._status = status if isinstance(status, dict) else {}
          self._error = ""
          report = wants_result or self._refresh_phase == "busy"
          if report:
            self._refresh_phase = "ok"
            self._refresh_stamp = time.strftime("%H:%M:%S")
            self._refresh_at = time.monotonic()
            self._refresh_msg = "已刷新 · %d 个模型" % len(entries)
      except Exception as exc:
        with self._lock:
          self._error = f"网页服务未响应：{type(exc).__name__}"
          report = wants_result or self._refresh_phase == "busy"
          if report:
            self._refresh_phase = "fail"
            self._refresh_stamp = time.strftime("%H:%M:%S")
            self._refresh_at = time.monotonic()
            self._refresh_msg = f"刷新失败：{type(exc).__name__}"
      finally:
        self._refreshing = False

    threading.Thread(target=run, name="model-picker-refresh", daemon=True).start()

  def _on_refresh(self):
    self._last_refresh = 0.0
    self._refresh_async(force=True, manual=True)

  def _on_pick(self, index: int):
    entry = self._entry(index)
    if not entry or self._picking:
      return
    sha = entry.get("sha256")
    if not sha or sha == self._selected:
      return
    self._picking = True
    self._pick_error = ""

    def run():
      try:
        result = _post_json(SELECT_URL, {"sha256": sha, "reboot": False})
        if not result.get("ok"):
          self._pick_error = "选择失败：" + str(result.get("error") or "未知原因")
      except Exception as exc:
        self._pick_error = f"选择失败：{type(exc).__name__}"
      finally:
        self._picking = False
        self._last_refresh = 0.0

    threading.Thread(target=run, name="model-picker-select", daemon=True).start()

  def _can_restart(self) -> bool:
    if self._error or self._status.get("state") in BUSY_STATES:
      return False
    if self._status.get("engaged"):
      return False
    try:
      return not ui_state.is_onroad()
    except Exception:
      return False

  def _on_reboot(self):
    def confirm(result: DialogResult):
      if result == DialogResult.CONFIRM:
        ui_state.params.put_bool("DoReboot", True)

    gui_app.push_widget(ConfirmDialog("确定重启设备以加载新模型？", "重启", callback=confirm))

  # ---- 显示文案 -----------------------------------------------------------
  def _entry(self, index: int) -> dict | None:
    try:
      return self._models[index]
    except (IndexError, TypeError):
      return None

  def _refresh_button_text(self) -> str:
    if self._refresh_phase == "busy":
      return "刷新中…"
    if self._refresh_phase in ("ok", "fail") and time.monotonic() - self._refresh_at < 8.0:
      return "已刷新" if self._refresh_phase == "ok" else "刷新失败"
    return "刷新"

  def _library_text(self) -> str:
    if self._refresh_phase == "busy":
      return "正在刷新模型库…"
    if self._error:
      return self._error
    source = "在线" if self._remote_catalog else "离线（仅本机缓存）"
    stamp = f" · {self._refresh_stamp} 已刷新" if self._refresh_stamp else ""
    return f"{source} · {len(self._models)} 个模型{stamp}"

  def _selected_text(self) -> str:
    sha = self._selected
    if not sha:
      return "未选择"
    for entry in self._models:
      if entry.get("sha256") == sha:
        return str(entry.get("label") or sha)[:24]
    return str(sha)[:12]

  def _state_text(self) -> str:
    if self._error:
      return "读取失败"
    if self._pick_error:
      return self._pick_error
    return status_text(self._status, self._selected)

  def _run_text(self) -> str:
    if not self._usbgpu:
      return "状态未知"
    if self._usbgpu.get("active"):
      return "运行中"
    if self._usbgpu.get("loading"):
      return "加载中…"
    if self._usbgpu.get("present"):
      return "已连接，未激活"
    return "未连接（仍可下载，但不会启用）"

  def _progress_pct(self) -> float:
    status = self._status
    if not status:
      return 0.0
    sha, selected = status.get("sha256"), self._selected
    if sha and selected and sha != selected and status.get("state") in BUSY_STATES:
      return 0.0
    try:
      return float(status.get("progress") or 0.0)
    except (TypeError, ValueError):
      return 0.0

  def _progress_text(self) -> str:
    return progress_text(self._status, self._selected)

  def _row_title(self, index: int) -> str:
    entry = self._entry(index)
    return model_title(entry, self._selected) if entry else ""

  def _row_button(self, index: int) -> str:
    entry = self._entry(index)
    return model_button_text(entry, self._selected) if entry else "—"

  def _row_value(self, index: int) -> str:
    entry = self._entry(index)
    if not entry:
      return ""
    return model_value_text(entry, self._running, self._dates.get(entry.get("sha256")))

  def _row_desc(self, index: int) -> str:
    entry = self._entry(index)
    if not entry:
      return ""
    sha = str(entry.get("sha256") or "")
    date = self._dates.get(sha)
    head = f"上游提交 {date}" if date else "上游提交日期未知"
    text = f"{head} · 标识 {sha[:12]} · 点击后设为当前模型"
    if not entry.get("downloaded"):
      text += "并开始下载"
    note = (entry.get("delivery_state") or {}).get("note") or ""
    return text + (" · " + note if note else "")

  def _row_enabled(self, index: int) -> bool:
    entry = self._entry(index)
    if not entry or self._picking:
      return False
    return entry.get("sha256") != self._selected

  # ---- Widget 生命周期 ----------------------------------------------------
  def show_event(self):
    super().show_event()
    self._visible = True
    self._refresh_async(force=True, manual=True)
    self._scroller.show_event()

  def hide_event(self):
    super().hide_event()
    self._visible = False
    self._scroller.hide_event()

  def _update_state(self):
    if self._visible:
      self._refresh_async()

  def _render(self, rect: rl.Rectangle):
    self._scroller.render(rect)
