"""
萝卜（Carrot）设置面板 —— 把 web 端 carrot_settings.json 的全部参数在设备 UI 上可视化。

与网页端同源（同一个 ``selfdrive/carrot_settings.json``），因此本文件不自定义任何
分类或取名规则，只复刻网页端既有规则：

* 分类树 ``分类 > 组 > 分节``
  复刻 ``carrot/server/services/settings.py::build_menu_categories()``：menu 子树递归
  摊平，只有带 ``params`` 的叶子成为一节，深层路径的标签用 ``" · "`` 串联作为分节标题
  （例如「高级扭矩 · 扭矩系数」）。所在组直接挂参数的，归一化为单个无标题分节。

* 中文标签
  分类/组/分节取节点的 ``zh``；参数标题/描述取 ``ctitle``/``cdescr``（等价于网页端
  ``localizedSettingItemText(item, "title", "etitle", ...)`` 的 zh 分支）。

* 控件类型
  复刻 ``selfdrive/carrot/web/js/pages/setting.js::getSettingControlConfig()``：
  ``control`` 字段优先；否则 ``min==0 && max==1`` -> 开关，选项数 2~4 -> 分段，
  5~8 -> 下拉，其余 -> 滑条（网页端滑条也是紧凑步进器，这里同样按步进器渲染）。
  ``params_keys.h`` 里声明为 BOOL 的强制为开关；声明为 STRING 的走字符串下拉
  （``STRING_CHOICES``，对齐网页端 ``getSoundLanguageSettingOptions()``）。

* 步长与量纲
  ``unit`` 是步长（网页端同名概念），量纲由 ``display_unit`` 决定；
  取值表与网页端 ``SETTING_DISPLAY_UNIT_TYPES`` 一致，表外一律 ``raw``。

* 选项名
  ``options`` 为 ``{ko, en, zh}``，按当前语言取（本机为 zh-CHS -> 取 ``zh``），
  与网页端 ``getDeclaredSettingOptionLabel()`` 一致；无 ``options`` 时显示原始数值。

参数合法性由运行时 ``Params.check_key() / get_type()`` 判定：不在 ``params_keys.h``
白名单内的参数会跳过（例如 ShowPathEnd / ShowRouteInfo，carrot 后端没有对应键，
写了也不生效）；未知的 STRING 参数同样跳过，避免把字符串当整数误写。

排版规则（统一排版）
--------------------
整个面板只有 **三档字号**（都是固定常量，与文字长短无关）和 **一个间距**：

* ``LABEL_SIZE`` —— 分类栏 / 分组栏 / 分节标题 / 参数行标题，全部同一个字号；
* ``VALUE_SIZE`` —— 控件里的数值与选项文本；
* ``DESC_SIZE``  —— 参数描述（展开后的小字）；
* ``GAP``        —— 按钮之间、栏之间、栏与列表之间、列表条目之间，全部同一个间距。

两条硬规则：

1. **标签永不按文字长度逐条缩放字号**（旧版会把放不下的标签单独调小，于是同一屏里
   出现好几种字号）。放不下时只做两件事：整栏折行（``_bar_rows``）或整栏统一降档
   （下限 ``LABEL_SIZE_MIN``）；再不行才用 ``…`` 截断，字号始终不变。
2. **栏按钮一律等宽**，分类栏与分组栏按钮同高（``BAR_BTN_HEIGHT``）；一行放不下就
   折成多行并把各行数量摊匀，保证「大小一致、间距一致」。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pyray as rl

from openpilot.common.params import ParamKeyType, Params, UnknownKeyName
from openpilot.system.ui.lib.application import FontWeight, gui_app, font_fallback
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.lib.wrap_text import wrap_text
from openpilot.system.ui.widgets import DialogResult, Widget
from openpilot.system.ui.widgets.label import gui_label, gui_text_box
from openpilot.system.ui.widgets.list_view import ItemAction
from openpilot.system.ui.widgets.option_dialog import MultiOptionDialog
from openpilot.system.ui.widgets.scroller_tici import Scroller
from openpilot.system.ui.widgets.toggle import HEIGHT as TOGGLE_HEIGHT
from openpilot.system.ui.widgets.toggle import Toggle, WIDTH as TOGGLE_WIDTH

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

# carrot.py 位于 .../selfdrive/ui/layouts/settings/  ->  parents[3] == .../selfdrive
SELFDRIVE_DIR = Path(__file__).resolve().parents[3]
SETTINGS_PATH = SELFDRIVE_DIR / "carrot_settings.json"

# ---- 统一排版：字号只有三档 -------------------------------------------------
LABEL_SIZE = 40        # ★ 所有菜单标签（分类栏 / 分组栏 / 分节标题 / 参数行标题）
VALUE_SIZE = 36        # ★ 控件内文本（步进值、+/-、分段选项、下拉当前值）
DESC_SIZE = 32         #   参数描述（展开后的说明文字）
LABEL_SIZE_MIN = 24    # 整栏统一降档时的下限（不用于逐条缩放）

# ---- 统一排版：间距只有一个 -------------------------------------------------
GAP = 12               # ★ 按钮之间 / 栏之间 / 栏与列表之间 / 条目之间，全是它
BAR_BTN_HEIGHT = 88    # 分类栏与分组栏按钮同高
CHIP_PAD = 18          # 栏按钮内左右留白
ROW_HEIGHT = 116       # 参数行高
HEADER_HEIGHT = 64     # 分节标题高（同字号同间距，只是更紧凑以省纵向空间）
ROW_PAD = 24           # 条目内左右留白
ROW_RADIUS = 0.35

# ---- 控件几何（同一行内控件等高，与 Toggle 的 80 对齐） ---------------------
CTRL_BUTTON_HEIGHT = 80
STEP_BUTTON_WIDTH = 76
SEGMENT_BUTTON_WIDTH = 118
SEGMENT_BUTTON_GAP = 12
SELECT_BUTTON_WIDTH = 240
STEP_VALUE_MIN_WIDTH = 150
STEP_VALUE_MAX_WIDTH = 300

TEXT_NORMAL = rl.Color(255, 255, 255, 255)
TEXT_DIM = rl.Color(150, 150, 150, 255)
TEXT_DESC = rl.Color(140, 140, 140, 255)
TEXT_RISK = rl.Color(255, 168, 64, 255)
ROW_BG = rl.Color(48, 48, 48, 255)
BAR_BG = rl.Color(48, 48, 48, 255)
BAR_BG_HOVER = rl.Color(66, 66, 66, 255)
BAR_BG_ACTIVE = rl.Color(51, 171, 76, 255)
CTRL_BG = rl.Color(57, 57, 57, 255)
CTRL_BG_HOVER = rl.Color(74, 74, 74, 255)
CTRL_BG_DISABLED = rl.Color(45, 45, 45, 160)
SECTION_COLOR = rl.Color(120, 170, 255, 255)

# 与网页端 setting.js::SETTING_DISPLAY_UNIT_TYPES 一致（表外一律按 raw 处理）
DISPLAY_UNITS = {
  "raw": "",
  "speedKph": "km/h",
  "distanceCm": "cm",
  "timeSec": "s",
  "timeMin": "min",
  "percent": "%",
  "degree": "deg",
}

CONTROL_KINDS = ("toggle", "segmented", "select", "slider")

# params_keys.h 里声明为 STRING 的参数：网页端有专门处理（setting.js::getSoundLanguageSettingOptions）
STRING_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
  "SoundLanguageSetting": (("auto", "自动"), ("en", "English"), ("ko", "한국어"), ("zh-CHS", "中文")),
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ParamDef:
  name: str
  title: str
  descr: str
  kind: str
  minimum: int = 0
  maximum: int = 0
  default: int = 0
  step: int = 1
  options: list[str] = field(default_factory=list)
  choices: tuple[tuple[str, str], ...] = ()
  display_unit: str = "raw"
  risk: bool = False
  default_str: str = ""

  @property
  def is_string(self) -> bool:
    return self.kind == "string_select"

  @property
  def option_count(self) -> int:
    if self.is_string:
      return len(self.choices)
    return self.maximum - self.minimum + 1

  def option_label(self, value) -> str:
    if self.is_string:
      return self.choice_label(str(value))
    idx = int(value) - self.minimum
    if self.options and 0 <= idx < len(self.options):
      return self.options[idx]
    suffix = DISPLAY_UNITS.get(self.display_unit, "")
    return f"{value}{suffix}" if suffix else str(value)

  def choice_label(self, code: str) -> str:
    for value, label in self.choices:
      if value == code:
        return label
    return code

  def option_values(self) -> list[str]:
    """下拉可选的「值」序列（字符串型为代码，数值型为 min..max）"""
    if self.is_string:
      return [value for value, _ in self.choices]
    return [str(v) for v in range(self.minimum, self.maximum + 1)]


@dataclass
class Section:
  id: str
  title: str
  params: list[str]


@dataclass
class MidGroup:
  id: str
  title: str
  sections: list[Section]


@dataclass
class Category:
  id: str
  title: str
  mids: list[MidGroup]


# ---------------------------------------------------------------------------
# 目录解析（严格复刻服务端 / 网页端规则）
# ---------------------------------------------------------------------------


def _join_labels(nodes: list[dict], key: str = "zh") -> str:
  """服务端 build_menu_categories.join_labels()：串起路径上的非空标签"""
  parts = [str(n.get(key) or "").strip() for n in nodes]
  return " · ".join(p for p in parts if p)


def _sections_from(nodes: list[dict], known: dict[str, ParamDef], parents: tuple = ()) -> list[Section]:
  """递归摊平 menu 子树：只有带 params 的叶子成为一节（同服务端 sections_from()）"""
  sections: list[Section] = []
  for node in nodes:
    path = parents + (node,)
    children = node.get("groups") or []
    if children:
      sections.extend(_sections_from(children, known, path))
      continue
    items = [n for n in (node.get("params") or []) if n in known]
    if not items:
      continue
    sections.append(Section(
      id="__".join(str(n.get("id") or "") for n in path if n.get("id")),
      title=_join_labels(path),
      params=items,
    ))
  return sections


def _build_categories(data: dict, known: dict[str, ParamDef]) -> list[Category]:
  """复刻服务端 build_menu_categories()：分类 > 组 > 分节"""
  categories: list[Category] = []
  for cat in data.get("menu") or []:
    mids: list[MidGroup] = []
    for grp in cat.get("groups") or []:
      if "groups" in grp:
        sections = _sections_from(grp.get("groups") or [], known)
      else:
        # 组里直接挂参数 -> 单个无标题分节（同服务端）
        sections = [Section(
          id=str(grp.get("id") or ""),
          title="",
          params=[n for n in (grp.get("params") or []) if n in known],
        )]
      mids.append(MidGroup(
        id=str(grp.get("id") or ""),
        title=str(grp.get("zh") or grp.get("en") or grp.get("ko") or grp.get("id") or ""),
        sections=[s for s in sections if s.params],
      ))
    categories.append(Category(
      id=str(cat.get("id") or ""),
      title=str(cat.get("zh") or cat.get("en") or cat.get("ko") or cat.get("id") or ""),
      mids=[m for m in mids if m.sections],
    ))
  return [c for c in categories if c.mids]


def _numeric_kind(p: dict, key_type: ParamKeyType) -> tuple[str, int, int, int]:
  """复刻网页端 getSettingControlConfig()；返回 (kind, min, max, step)"""
  override = p.get("control") if p.get("control") in CONTROL_KINDS else None
  try:
    mn = int(p.get("min"))
    mx = int(p.get("max"))
  except (TypeError, ValueError):
    mn = mx = 0
  try:
    step = max(1, int(p.get("unit") or 1))  # unit 是步长，不是量纲
  except (TypeError, ValueError):
    step = 1
  option_count = max(0, mx - mn + 1)

  # 2026-10-17：显式control 覆盖优先于BOOL 判定。
  # 原因：BOOL 参数在 params_keys.h 里是编译期类型，但其中一部分（0/1 语义枚举）
  # 需要可点选的数字形式。原先 BOOL 无条件赢，导致 carrot_settings.json 里写的
  # "control": "segmented" 对 BOOL 完全失效。现改为「显式声明优先」。
  if override is not None:
    kind = override
  elif key_type == ParamKeyType.BOOL:
    kind = "toggle"
  elif mn == 0 and mx == 1:
    kind = "toggle"
  elif 2 <= option_count <= 4:
    kind = "segmented"
  elif 4 < option_count <= 8:
    kind = "select"
  else:
    kind = "slider"
  if kind == "slider":
    kind = "stepper"  # 网页端把 slider 也渲染成紧凑步进器
  return kind, mn, mx, step


def _make_def(p: dict, key_type: ParamKeyType) -> ParamDef | None:
  name = str(p.get("name") or "")
  title = str(p.get("ctitle") or p.get("etitle") or p.get("title") or name)
  descr = str(p.get("cdescr") or p.get("edescr") or p.get("descr") or "")
  display_unit = str(p.get("display_unit") or "raw")
  if display_unit not in DISPLAY_UNITS:
    display_unit = "raw"
  risk = p.get("risk") == "high"

  if key_type == ParamKeyType.STRING:
    choices = STRING_CHOICES.get(name)
    if not choices:
      return None  # 未知字符串参数：设备端无法安全编辑，跳过
    return ParamDef(
      name=name, title=title, descr=descr, kind="string_select",
      default_str=str(p.get("default") or ""), choices=choices,
      display_unit=display_unit, risk=risk,
    )

  kind, mn, mx, step = _numeric_kind(p, key_type)
  options: list[str] = []
  raw_options = p.get("options")
  if isinstance(raw_options, dict):
    options = list(raw_options.get("zh") or raw_options.get("en") or raw_options.get("ko") or [])
  try:
    default = int(p.get("default") or 0)
  except (TypeError, ValueError):
    default = 0
  return ParamDef(
    name=name, title=title, descr=descr, kind=kind,
    minimum=mn, maximum=mx, default=default, step=step,
    options=options, display_unit=display_unit, risk=risk,
  )


def load_catalog(params: Params, path: Path | str = SETTINGS_PATH) -> tuple[list[Category], dict[str, ParamDef]]:
  """解析 carrot_settings.json -> (分类树, 参数表)"""
  with open(path, encoding="utf-8") as f:
    data = json.load(f)

  param_defs: dict[str, ParamDef] = {}
  for p in data.get("params", []):
    name = p.get("name")
    if not name:
      continue
    try:
      key_type = params.get_type(name)
    except UnknownKeyName:
      continue  # 不在 params_keys.h 白名单：后端没有这个键，展示也无法写入
    except Exception:
      key_type = ParamKeyType.INT
    pdef = _make_def(p, key_type)
    if pdef is not None:
      param_defs[name] = pdef

  categories = _build_categories(data, param_defs)

  # 安全网：万一将来 menu 漏引用了参数，也不能凭空丢失（当前数据为 0 个）
  referenced = {n for c in categories for m in c.mids for s in m.sections for n in s.params}
  leftovers = [n for n in param_defs if n not in referenced]
  if leftovers:
    extra = MidGroup(id="__EXTRA__", title="", sections=[Section(id="__EXTRA__", title="", params=leftovers)])
    if categories:
      categories[-1].mids.append(extra)
    else:
      categories = [Category(id="__EXTRA__", title=tr("Uncategorized"), mids=[extra])]

  return categories, param_defs


# ---------------------------------------------------------------------------
# 参数读写
# ---------------------------------------------------------------------------


class ParamStore:
  """参数读写 + 内存缓存（避免每帧读磁盘）"""

  def __init__(self, params: Params):
    self._params = params
    self._cache: dict[str, int | str] = {}

  def _raw(self, name: str):
    try:
      return self._params.get(name, return_default=True)
    except TypeError:
      return self._params.get(name)

  def get_int(self, name: str, default: int = 0) -> int:
    cached = self._cache.get(name)
    if isinstance(cached, int):
      return cached
    raw = self._raw(name)
    if isinstance(raw, bytes):
      raw = raw.decode(errors="replace")
    try:
      value = int(float(str(raw).strip() or default))
    except (TypeError, ValueError):
      value = default
    self._cache[name] = value
    return value

  def get_str(self, name: str, default: str = "") -> str:
    cached = self._cache.get(name)
    if isinstance(cached, str):
      return cached
    raw = self._raw(name)
    if isinstance(raw, bytes):
      raw = raw.decode(errors="replace")
    value = str(raw).strip() if raw is not None else ""
    value = value or default
    self._cache[name] = value
    return value

  def set_int(self, name: str, value: int) -> None:
    value = int(value)
    self._cache[name] = value
    try:
      self._params.put(name, str(value))
    except Exception:
      try:
        self._params.put_int(name, value)
      except Exception:
        pass

  def set_str(self, name: str, value: str) -> None:
    self._cache[name] = value
    try:
      self._params.put(name, value)
    except Exception:
      pass

  def invalidate(self) -> None:
    self._cache.clear()


def _truncate(font, text: str, size: int, max_width: float, keep: int = 2) -> str:
  """只截断、不缩放：字号恒定，超宽时以 … 收尾。"""
  measured = measure_text_cached(font, text, size)
  if measured.x <= max_width:
    return text
  label = text
  while measured.x > max_width and len(label) > keep:
    label = label[:-1]
    measured = measure_text_cached(font, label + "…", size)
  return label + "…" if label != text else text


# ---------------------------------------------------------------------------
# 右侧控件
# ---------------------------------------------------------------------------


class StepperAction(ItemAction):
  """``-  值  +`` 紧凑步进器（对应网页端 stepper）"""

  def __init__(self, store: ParamStore, pdef: ParamDef):
    self._store = store
    self._pdef = pdef
    self._font = font_fallback(gui_app.font(FontWeight.MEDIUM))
    self._value_width = self._measure_value_width()
    super().__init__(width=STEP_BUTTON_WIDTH * 2 + self._value_width)

  def _measure_value_width(self) -> float:
    """按最长可能文本量出数值区宽度。

    量纲后缀（如 -30km/h、100%）比裸数字宽得多，固定宽度会把数值挤到 +/- 上。
    """
    widths = [measure_text_cached(self._font, self._pdef.option_label(v), VALUE_SIZE).x
              for v in (self._pdef.minimum, self._pdef.maximum, self._pdef.default)]
    return max(STEP_VALUE_MIN_WIDTH, min(STEP_VALUE_MAX_WIDTH, max(widths, default=0.0) + 28))

  def value(self) -> int:
    return self._store.get_int(self._pdef.name, self._pdef.default)

  def _button_rects(self) -> tuple[rl.Rectangle, rl.Rectangle]:
    y = self._rect.y + (self._rect.height - CTRL_BUTTON_HEIGHT) / 2
    minus = rl.Rectangle(self._rect.x, y, STEP_BUTTON_WIDTH, CTRL_BUTTON_HEIGHT)
    plus = rl.Rectangle(self._rect.x + self._rect.width - STEP_BUTTON_WIDTH, y, STEP_BUTTON_WIDTH, CTRL_BUTTON_HEIGHT)
    return minus, plus

  def _apply(self, direction: int) -> None:
    current = self.value()
    target = max(self._pdef.minimum, min(self._pdef.maximum, current + direction * (self._pdef.step or 1)))
    if target != current:
      self._store.set_int(self._pdef.name, target)

  def _render(self, rect: rl.Rectangle) -> bool:
    current = self.value()
    minus_rect, plus_rect = self._button_rects()
    value_rect = rl.Rectangle(minus_rect.x + STEP_BUTTON_WIDTH, rect.y, self._value_width, rect.height)

    mouse = rl.get_mouse_position()
    can_minus = current > self._pdef.minimum
    can_plus = current < self._pdef.maximum

    for btn_rect, label, enabled in ((minus_rect, "-", can_minus), (plus_rect, "+", can_plus)):
      hovered = enabled and rl.check_collision_point_rec(mouse, btn_rect)
      bg = CTRL_BG_HOVER if hovered else (CTRL_BG if enabled else CTRL_BG_DISABLED)
      rl.draw_rectangle_rounded(btn_rect, 0.9, 20, bg)
      size = measure_text_cached(self._font, label, VALUE_SIZE)
      rl.draw_text_ex(
        self._font, label,
        rl.Vector2(btn_rect.x + (btn_rect.width - size.x) / 2, btn_rect.y + (btn_rect.height - size.y) / 2),
        VALUE_SIZE, 0, TEXT_NORMAL if enabled else TEXT_DIM,
      )

    text = self._pdef.option_label(current)
    size = measure_text_cached(self._font, text, VALUE_SIZE)
    rl.draw_text_ex(
      self._font, text,
      rl.Vector2(value_rect.x + (value_rect.width - size.x) / 2, value_rect.y + (value_rect.height - size.y) / 2),
      VALUE_SIZE, 0, TEXT_NORMAL,
    )
    return False

  def _handle_mouse_release(self, mouse_pos) -> None:
    if not self.enabled:
      return
    minus_rect, plus_rect = self._button_rects()
    if rl.check_collision_point_rec(mouse_pos, minus_rect):
      self._apply(-1)
    elif rl.check_collision_point_rec(mouse_pos, plus_rect):
      self._apply(1)


class SegmentAction(ItemAction):
  """2~4 个互斥按钮（对应网页端 segmented）"""

  def __init__(self, store: ParamStore, pdef: ParamDef):
    count = max(1, pdef.option_count)
    super().__init__(width=count * SEGMENT_BUTTON_WIDTH + (count - 1) * SEGMENT_BUTTON_GAP)
    self._store = store
    self._pdef = pdef
    self._font = font_fallback(gui_app.font(FontWeight.MEDIUM))

  def value(self) -> int:
    return self._store.get_int(self._pdef.name, self._pdef.default)

  def _button_rects(self) -> list[rl.Rectangle]:
    y = self._rect.y + (self._rect.height - CTRL_BUTTON_HEIGHT) / 2
    return [
      rl.Rectangle(self._rect.x + i * (SEGMENT_BUTTON_WIDTH + SEGMENT_BUTTON_GAP), y,
                   SEGMENT_BUTTON_WIDTH, CTRL_BUTTON_HEIGHT)
      for i in range(self._pdef.option_count)
    ]

  def _render(self, rect: rl.Rectangle) -> bool:
    current = self.value()
    mouse = rl.get_mouse_position()
    for i, btn_rect in enumerate(self._button_rects()):
      opt_value = self._pdef.minimum + i
      selected = opt_value == current
      hovered = rl.check_collision_point_rec(mouse, btn_rect) and self.enabled
      if selected:
        bg = BAR_BG_ACTIVE
      elif hovered:
        bg = CTRL_BG_HOVER
      else:
        bg = CTRL_BG
      rl.draw_rectangle_rounded(btn_rect, 0.9, 20, bg)

      label = _truncate(self._font, self._pdef.option_label(opt_value), VALUE_SIZE, btn_rect.width - 12, keep=1)
      measured = measure_text_cached(self._font, label, VALUE_SIZE)
      rl.draw_text_ex(
        self._font, label,
        rl.Vector2(btn_rect.x + (btn_rect.width - measured.x) / 2, btn_rect.y + (btn_rect.height - measured.y) / 2),
        VALUE_SIZE, 0, TEXT_NORMAL if self.enabled else TEXT_DIM,
      )
    return False

  def _handle_mouse_release(self, mouse_pos) -> None:
    if not self.enabled:
      return
    for i, btn_rect in enumerate(self._button_rects()):
      if rl.check_collision_point_rec(mouse_pos, btn_rect):
        target = self._pdef.minimum + i
        if target != self.value():
          self._store.set_int(self._pdef.name, target)
        return


class ToggleActionEx(ItemAction):
  """开关（对应网页端 toggle；BOOL 与 0/1 范围参数都用它）"""

  def __init__(self, store: ParamStore, pdef: ParamDef):
    super().__init__(width=TOGGLE_WIDTH)
    self._store = store
    self._pdef = pdef
    self._toggle = Toggle(initial_state=store.get_int(pdef.name, pdef.default) == 1, callback=self._on_toggle)

  def _on_toggle(self, state: bool) -> None:
    self._store.set_int(self._pdef.name, 1 if state else 0)

  def sync(self) -> None:
    self._toggle.set_state(self._store.get_int(self._pdef.name, self._pdef.default) == 1)

  def set_touch_valid_callback(self, touch_callback: Callable[[], bool]) -> None:
    super().set_touch_valid_callback(touch_callback)
    self._toggle.set_touch_valid_callback(touch_callback)

  def _render(self, rect: rl.Rectangle) -> bool:
    self._toggle.set_enabled(self.enabled)
    return bool(
      self._toggle.render(
        rl.Rectangle(rect.x + self._rect.width - TOGGLE_WIDTH,
                     rect.y + (rect.height - TOGGLE_HEIGHT) / 2,
                     TOGGLE_WIDTH, TOGGLE_HEIGHT)
      )
    )


class SelectAction(ItemAction):
  """下拉（对应网页端 select），点击弹出选单；数值型与字符串型共用"""

  def __init__(self, store: ParamStore, pdef: ParamDef, on_open: Callable[[ParamDef], None]):
    super().__init__(width=SELECT_BUTTON_WIDTH)
    self._store = store
    self._pdef = pdef
    self._on_open = on_open
    self._font = font_fallback(gui_app.font(FontWeight.MEDIUM))

  def current_label(self) -> str:
    if self._pdef.is_string:
      return self._pdef.choice_label(self._store.get_str(self._pdef.name, self._pdef.default_str))
    return self._pdef.option_label(self._store.get_int(self._pdef.name, self._pdef.default))

  def _button_rect(self) -> rl.Rectangle:
    y = self._rect.y + (self._rect.height - CTRL_BUTTON_HEIGHT) / 2
    return rl.Rectangle(self._rect.x + self._rect.width - SELECT_BUTTON_WIDTH, y,
                        SELECT_BUTTON_WIDTH, CTRL_BUTTON_HEIGHT)

  def _draw_caret(self, center: rl.Vector2, size: float, color: rl.Color) -> None:
    """下拉指示三角：用几何绘制而非字形。

    两个坑，都已实测：
    1. KaiGen 字体没有 ▾/▼ 字形，缺字形时 raylib 会兜底画成 '?'，所以不能用字符。
    2. comma 的 raylib 构建里 DrawTriangle 是静默无效的（pyray 包装器与底层 cffi
       直调都画出 0 像素，而 draw_circle_v / draw_rectangle / draw_line_ex 正常），
       所以改用 draw_poly 画正三边形。
    sides=3 + rotation=90° => 顶点落在正下方，得到尖朝下的三角。
    """
    rl.draw_poly(center, 3, size * 0.5, 90.0, color)

  def _render(self, rect: rl.Rectangle) -> bool:
    btn_rect = self._button_rect()
    color = TEXT_NORMAL if self.enabled else TEXT_DIM

    hovered = rl.check_collision_point_rec(rl.get_mouse_position(), btn_rect) and self.enabled
    rl.draw_rectangle_rounded(btn_rect, 0.9, 20, CTRL_BG_HOVER if hovered else CTRL_BG)

    caret_size = 18.0
    caret_pad = 16.0
    text_area = btn_rect.width - caret_size - caret_pad * 2

    label = _truncate(self._font, self.current_label(), VALUE_SIZE, text_area, keep=2)
    measured = measure_text_cached(self._font, label, VALUE_SIZE)

    rl.draw_text_ex(
      self._font, label,
      rl.Vector2(btn_rect.x + caret_pad + (text_area - measured.x) / 2,
                 btn_rect.y + (btn_rect.height - measured.y) / 2),
      VALUE_SIZE, 0, color,
    )
    self._draw_caret(
      rl.Vector2(btn_rect.x + btn_rect.width - caret_pad - caret_size / 2, btn_rect.y + btn_rect.height / 2),
      caret_size, color,
    )
    return False

  def _handle_mouse_release(self, mouse_pos) -> None:
    if not self.enabled:
      return
    if rl.check_collision_point_rec(mouse_pos, self._button_rect()):
      self._on_open(self._pdef)


# ---------------------------------------------------------------------------
# 行 / 分节标题
# ---------------------------------------------------------------------------


class SectionHeader(Widget):
  """分节标题：与参数行同字号、同左右留白、同间距，只是更矮且透明底。"""

  def __init__(self, title: str):
    super().__init__()
    self._title = title
    self._font = font_fallback(gui_app.font(FontWeight.MEDIUM))
    self.set_rect(rl.Rectangle(0, 0, 1000, HEADER_HEIGHT))

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width

  def _render(self, _: rl.Rectangle) -> None:
    label = _truncate(self._font, self._title, LABEL_SIZE, self._rect.width - ROW_PAD * 2, keep=3)
    size = measure_text_cached(self._font, label, LABEL_SIZE)
    rl.draw_text_ex(
      self._font, label,
      rl.Vector2(self._rect.x + ROW_PAD, self._rect.y + (self._rect.height - size.y) / 2),
      LABEL_SIZE, 0, SECTION_COLOR,
    )


class ParamRow(Widget):
  """一行参数：左侧标题（点按展开描述）+ 右侧控件"""

  def __init__(self, pdef: ParamDef, store: ParamStore, on_open_select: Callable[[ParamDef], None]):
    super().__init__()
    self._pdef = pdef
    self._store = store
    self._expanded = False
    self._title_font = font_fallback(gui_app.font(FontWeight.MEDIUM))
    self._title_area_action_w = 0.0  # 本组最宽控件宽，由 CarrotLayout 统一下发

    kind = pdef.kind
    action: ItemAction
    if kind == "toggle":
      action = ToggleActionEx(store, pdef)
    elif kind == "segmented":
      action = SegmentAction(store, pdef)
    elif kind in ("select", "string_select"):
      action = SelectAction(store, pdef, on_open_select)
    else:
      action = StepperAction(store, pdef)
    self.action_item = action
    self.set_rect(rl.Rectangle(0, 0, 1000, ROW_HEIGHT))

  @property
  def pdef(self) -> ParamDef:
    return self._pdef

  def set_title_area(self, max_action_width: float) -> None:
    """由 CarrotLayout 在 rebuild 时统一下发，保证全表标题可用宽度一致。"""
    self._title_area_action_w = max(0.0, float(max_action_width))

  def sync(self) -> None:
    if isinstance(self.action_item, ToggleActionEx):
      self.action_item.sync()

  def set_touch_valid_callback(self, touch_callback: Callable[[], bool]) -> None:
    super().set_touch_valid_callback(touch_callback)
    if self.action_item:
      self.action_item.set_touch_valid_callback(touch_callback)

  def set_parent_rect(self, parent_rect: rl.Rectangle) -> None:
    super().set_parent_rect(parent_rect)
    self._rect.width = parent_rect.width
    self._rect.height = self._current_height()

  def _current_height(self) -> float:
    if not (self._expanded and self._pdef.descr):
      return ROW_HEIGHT
    # 用设备自带 wrap_text 按真实像素宽度折行，行数决定展开高度（与渲染一致）
    font = font_fallback(gui_app.font(FontWeight.NORMAL))
    max_w = max(40, int(self._rect.width - ROW_PAD * 2))
    lines = len(wrap_text(font, self._pdef.descr, DESC_SIZE, max_w, 0))
    return ROW_HEIGHT + min(320, 12 + lines * round(DESC_SIZE * 1.55))

  @property
  def _action_width(self) -> float:
    return self.action_item.get_width_hint() if self.action_item else 0

  def _action_rect(self) -> rl.Rectangle:
    return rl.Rectangle(self._rect.x + self._rect.width - self._action_width - ROW_PAD,
                        self._rect.y, self._action_width, ROW_HEIGHT)

  def _handle_mouse_release(self, mouse_pos) -> None:
    if not self._pdef.descr:
      return
    # 点在右侧控件区内不展开描述
    if rl.check_collision_point_rec(mouse_pos, self._action_rect()):
      return
    self._expanded = not self._expanded
    self._rect.height = self._current_height()

  def _render(self, _: rl.Rectangle) -> None:
    width = self._rect.width
    rl.draw_rectangle_rounded(self._rect, ROW_RADIUS, 16, ROW_BG)

    if self.action_item:
      self.action_item.render(self._action_rect())

    # 标题可用宽度按「本组最宽控件」统一预留，保证全表标题左边界与可用宽度一致
    reserved = max(self._action_width, self._title_area_action_w)
    max_title_width = width - reserved - ROW_PAD * 3
    if max_title_width < 160:
      max_title_width = 160

    label = _truncate(self._title_font, self._pdef.title, LABEL_SIZE, max_title_width, keep=4)
    measured = measure_text_cached(self._title_font, label, LABEL_SIZE)

    color = TEXT_RISK if self._pdef.risk else TEXT_NORMAL
    rl.draw_text_ex(
      self._title_font, label,
      rl.Vector2(self._rect.x + ROW_PAD, self._rect.y + (ROW_HEIGHT - measured.y) / 2),
      LABEL_SIZE, 0, color,
    )

    if self._expanded and self._pdef.descr:
      desc_rect = rl.Rectangle(self._rect.x + ROW_PAD, self._rect.y + ROW_HEIGHT - 6,
                               width - ROW_PAD * 2, self._rect.height - ROW_HEIGHT + 6)
      gui_text_box(desc_rect, self._pdef.descr, font_size=DESC_SIZE, color=TEXT_DESC,
                   font_weight=FontWeight.NORMAL, alignment=rl.GuiTextAlignment.TEXT_ALIGN_LEFT,
                   alignment_vertical=rl.GuiTextAlignmentVertical.TEXT_ALIGN_TOP,
                   line_scale=1.35)


# ---------------------------------------------------------------------------
# 主面板
# ---------------------------------------------------------------------------


class CarrotLayout(Widget):
  def __init__(self):
    super().__init__()
    self._params = Params()
    self._store = ParamStore(self._params)
    self._font_bar = font_fallback(gui_app.font(FontWeight.MEDIUM))

    try:
      self._categories, self._param_defs = load_catalog(self._params)
    except Exception:
      self._categories, self._param_defs = [], {}

    self._cat_idx = 0
    self._mid_idx = 0
    self._rows: list[Widget] = []
    self._scroller = Scroller([])
    self._category_rects: list[rl.Rectangle] = []
    self._mid_rects: list[rl.Rectangle] = []
    self._select_dialog: MultiOptionDialog | None = None

    self.set_rect(rl.Rectangle(0, 0, 1000, 700))
    self._rebuild()

  # -- 当前选择 ------------------------------------------------------------

  @property
  def _category(self) -> Category | None:
    if not self._categories:
      return None
    return self._categories[min(self._cat_idx, len(self._categories) - 1)]

  @property
  def _mid(self) -> MidGroup | None:
    cat = self._category
    if cat is None or not cat.mids:
      return None
    return cat.mids[min(self._mid_idx, len(cat.mids) - 1)]

  def _rebuild(self) -> None:
    rows: list[Widget] = []
    mid = self._mid
    if mid is not None:
      for section in mid.sections:
        if section.title:
          rows.append(SectionHeader(section.title))
        for name in section.params:
          pdef = self._param_defs.get(name)
          if pdef is not None:
            rows.append(ParamRow(pdef, self._store, self._on_open_select))

    if not rows:
      rows.append(SectionHeader(tr("No settings in this group")))

    # 标题栏统一按「本组最宽控件」预留，全表标题可用宽度一致（避免长短标题各自为政）
    max_action_w = max((r._action_width for r in rows if isinstance(r, ParamRow)), default=0.0)
    for row in rows:
      if isinstance(row, ParamRow):
        row.set_title_area(max_action_w)
        row.sync()
    self._rows = rows
    self._scroller = Scroller(rows, spacing=GAP, pad_end=False)

  # -- 栏按钮排版 ----------------------------------------------------------

  def _bar_row_fits(self, chunk: list[str], count: int, width: float, size: int) -> bool:
    """count 个等宽按钮放在 width 里，chunk 里的标签是否都能用 size 放下。"""
    if count <= 0:
      return True
    chip = (width - GAP * (count - 1)) / count
    avail = chip - CHIP_PAD * 2
    return all(measure_text_cached(self._font_bar, t, size).x <= avail for t in chunk)

  def _bar_rows(self, titles: list[str], width: float) -> tuple[list[list[str]], int]:
    """把一栏标签排成若干行「等宽按钮」，返回 (每行标签, 该栏统一字号)。

    规则（保证「标签大小一致」不靠逐条缩字号来兜）：
      1) 先试一行放下全部 -> 最常见，直接返回；
      2) 否则贪心分行（每行尽量多）；
      3) 再把各行数量摊匀（避免 4+2 这种形状，改成 3+3）；
      4) 单行只剩 1 个标签仍放不下时，才整栏统一降到能放下的字号（不逐条缩）。
    """
    if not titles:
      return [], LABEL_SIZE

    size = LABEL_SIZE
    while size > LABEL_SIZE_MIN:
      n = len(titles)
      if self._bar_row_fits(titles, n, width, size):
        return [list(titles)], size

      # 贪心：第一行尽量多
      k = n - 1
      while k > 1 and not self._bar_row_fits(titles[:k], k, width, size):
        k -= 1
      if k >= 2:
        rows = [list(titles[:k])]
        rest = list(titles[k:])
        while rest:
          j = len(rest)
          while j > 1 and not self._bar_row_fits(rest[:j], j, width, size):
            j -= 1
          rows.append(list(rest[:j]))
          rest = rest[j:]

        # 数量摊匀：n=6 且贪心得到 4+2 时，改成 3+3
        per = -(-n // len(rows))
        if per >= 2:
          even = [list(titles[i:i + per]) for i in range(0, n, per)]
          if all(self._bar_row_fits(c, len(c), width, size) for c in even):
            rows = even
        return rows, size

      size -= 2

    return [list(titles)], size

  def _draw_bar(self, x: float, y: float, width: float, titles: list[str], active: int,
                cache: list[rl.Rectangle]) -> float:
    """画一栏按钮，返回该栏总高；cache 按标签顺序填按钮 rect（用于命中判定）。"""
    cache.clear()
    rows, size = self._bar_rows(titles, width)
    if not rows:
      return 0.0

    mouse = rl.get_mouse_position()
    flat = 0
    for r, chunk in enumerate(rows):
      count = len(chunk)
      btn_width = (width - GAP * (count - 1)) / count
      row_y = y + r * (BAR_BTN_HEIGHT + GAP)
      for i, title in enumerate(chunk):
        btn_rect = rl.Rectangle(x + i * (btn_width + GAP), row_y, btn_width, BAR_BTN_HEIGHT)

        if flat == active:
          bg = BAR_BG_ACTIVE
        elif rl.check_collision_point_rec(mouse, btn_rect):
          bg = BAR_BG_HOVER
        else:
          bg = BAR_BG
        rl.draw_rectangle_rounded(btn_rect, 0.8, 16, bg)

        label = _truncate(self._font_bar, title, size, btn_rect.width - CHIP_PAD * 2, keep=2)
        measured = measure_text_cached(self._font_bar, label, size)
        rl.draw_text_ex(
          self._font_bar, label,
          rl.Vector2(btn_rect.x + (btn_rect.width - measured.x) / 2,
                     btn_rect.y + (btn_rect.height - measured.y) / 2),
          size, 0, TEXT_NORMAL,
        )
        cache.append(btn_rect)
        flat += 1

    return len(rows) * BAR_BTN_HEIGHT + (len(rows) - 1) * GAP

  # -- 交互 ----------------------------------------------------------------

  def _on_open_select(self, pdef: ParamDef) -> None:
    labels = [pdef.option_label(v) for v in pdef.option_values()]
    values = pdef.option_values()
    current = self._store.get_str(pdef.name, pdef.default_str) if pdef.is_string \
      else str(self._store.get_int(pdef.name, pdef.default))
    current_label = pdef.choice_label(current) if pdef.is_string else pdef.option_label(int(current))

    def on_result(result: DialogResult):
      if result != DialogResult.CONFIRM or self._select_dialog is None:
        return
      selection = self._select_dialog.selection
      if selection not in labels:
        return
      chosen = values[labels.index(selection)]
      if pdef.is_string:
        self._store.set_str(pdef.name, chosen)
      else:
        self._store.set_int(pdef.name, int(chosen))

    self._select_dialog = MultiOptionDialog(pdef.title, labels, current_label, callback=on_result)
    gui_app.push_widget(self._select_dialog)

  def show_event(self):
    super().show_event()
    self._store.invalidate()
    for row in self._rows:
      if isinstance(row, ParamRow):
        row.sync()
    self._scroller.show_event()

  def hide_event(self):
    super().hide_event()
    self._scroller.hide_event()

  def _handle_mouse_release(self, mouse_pos) -> None:
    for idx, rect in enumerate(self._category_rects):
      if rl.check_collision_point_rec(mouse_pos, rect):
        if idx != self._cat_idx:
          self._cat_idx = idx
          self._mid_idx = 0
          self._rebuild()
        return

    for idx, rect in enumerate(self._mid_rects):
      if rl.check_collision_point_rec(mouse_pos, rect):
        if idx != self._mid_idx:
          self._mid_idx = idx
          self._rebuild()
        return

  # -- 渲染 ----------------------------------------------------------------

  def _render(self, rect: rl.Rectangle) -> None:
    cat = self._category
    if cat is None:
      gui_label(rect, tr("Settings file not found"), font_size=48, color=TEXT_DIM,
                alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER)
      return

    cat_h = self._draw_bar(rect.x, rect.y, rect.width, [c.title for c in self._categories],
                           self._cat_idx, self._category_rects)

    mid_y = rect.y + cat_h + GAP
    mid_h = self._draw_bar(rect.x, mid_y, rect.width, [m.title for m in cat.mids],
                           self._mid_idx, self._mid_rects)

    list_y = mid_y + mid_h + GAP
    list_height = rect.y + rect.height - list_y
    if list_height > 0:
      self._scroller.render(rl.Rectangle(rect.x, list_y, rect.width, list_height))
