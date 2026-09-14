#!/usr/bin/env python3
import ast
from pathlib import Path
import json

import pyray as rl

FONT_DIR = Path(__file__).resolve().parent
SELFDRIVE_DIR = FONT_DIR.parents[1]
OPENPILOT_DIR = SELFDRIVE_DIR.parent
TRANSLATIONS_DIR = SELFDRIVE_DIR / "ui" / "translations"
LANGUAGES_FILE = TRANSLATIONS_DIR / "languages.json"
# 源码里的字符串字面量也算字形来源（见 _ui_source_chars）
UI_SOURCE_DIRS = (SELFDRIVE_DIR / "ui", OPENPILOT_DIR / "system" / "ui")

GLYPH_PADDING = 6
EXTRA_CHARS = "–‑✓×°§•X⚙✕◀▶✔⌫⇧␣○●↳çêüñ–‑✓×°§•€£¥"
UNIFONT_LANGUAGES = {"th", "zh-CHT", "zh-CHS", "ko", "ja"}


def _languages():
  if not LANGUAGES_FILE.exists():
    return {}
  with LANGUAGES_FILE.open(encoding="utf-8") as f:
    return json.load(f)


def _ui_source_chars() -> set:
  """UI 源码里出现的所有非 ASCII 字符（字符串字面量）。

  本 fork 的中文文案直接写在 .py 里（tr("中文") / tr_noop("中文")），
  未翻译时按字面显示。这些字符不在 .po 里，只看 .po 会漏掉 → 全新安装显示 "?"。
  这里取「所有字符串字面量的非 ASCII 字符」这个超集，不去匹配具体调用形式，
  这样 tr() 怎么导入、怎么嵌套都不会漏。
  """
  chars: set = set()
  files = 0
  for directory in UI_SOURCE_DIRS:
    if not directory.is_dir():
      continue
    for py in sorted(directory.rglob("*.py")):
      try:
        tree = ast.parse(py.read_text(encoding="utf-8"))
      except (SyntaxError, UnicodeDecodeError, OSError):
        continue
      files += 1
      for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
          chars.update(c for c in node.value if ord(c) > 127)

  print(f"UI source chars: {len(chars)} distinct from {files} .py files")
  return chars


def _char_sets():
  base = set(map(chr, range(32, 127))) | set(EXTRA_CHARS)
  labels = set(base)
  per_lang: dict[str, tuple[int, ...]] = {}

  # 只并入 OpFont(CJK 字体) 用的图集，不动 base —— base 会喂给 Inter 等
  # 纯拉丁字体，塞汉字进去既浪费烘焙时间又拿不到字形。
  ui_chars = _ui_source_chars()
  labels.update(ui_chars)

  for language, code in _languages().items():
    labels.update(language)
    po_path = TRANSLATIONS_DIR / f"app_{code}.po"
    try:
      chars = set(po_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
      continue
    if code in UNIFONT_LANGUAGES:
      lang_chars = set(base) | chars | ui_chars
      per_lang[code] = tuple(sorted(ord(c) for c in lang_chars))
    else:
      base.update(chars)

  base_cp = tuple(sorted(ord(c) for c in base))
  labels_cp = tuple(sorted(ord(c) for c in labels))
  return base_cp, labels_cp, per_lang


def _glyph_metrics(glyphs, rects, glyph_count: int):
  entries = []
  min_offset_y, max_extent = None, 0
  for idx in range(glyph_count):
    glyph = glyphs[idx]
    rect = rects[idx]
    width = int(round(rect.width))
    height = int(round(rect.height))
    offset_y = int(round(glyph.offsetY))
    min_offset_y = offset_y if min_offset_y is None else min(min_offset_y, offset_y)
    max_extent = max(max_extent, offset_y + height)
    entries.append({
      "id": glyph.value,
      "x": int(round(rect.x)),
      "y": int(round(rect.y)),
      "width": width,
      "height": height,
      "xoffset": int(round(glyph.offsetX)),
      "yoffset": offset_y,
      "xadvance": int(round(glyph.advanceX)),
    })

  if min_offset_y is None:
    raise RuntimeError("No glyphs were generated")

  line_height = int(round(max_extent - min_offset_y))
  base = int(round(max_extent))
  return entries, line_height, base


def _write_bmfont(path: Path, font_size: int, face: str, atlas_name: str, line_height: int, base: int, atlas_size, entries):
  # TODO: why doesn't raylib calculate these metrics correctly?
  if line_height != font_size:
    print("using font size for line height", atlas_name)
    line_height = font_size
  lines = [
    f"info face=\"{face}\" size=-{font_size} bold=0 italic=0 charset=\"\" unicode=1 stretchH=100 smooth=0 aa=1 padding=0,0,0,0 spacing=0,0 outline=0",
    f"common lineHeight={line_height} base={base} scaleW={atlas_size[0]} scaleH={atlas_size[1]} pages=1 packed=0 alphaChnl=0 redChnl=4 greenChnl=4 blueChnl=4",
    f"page id=0 file=\"{atlas_name}\"",
    f"chars count={len(entries)}",
  ]
  for entry in entries:
    lines.append(
      ("char id={id:<4} x={x:<5} y={y:<5} width={width:<5} height={height:<5} " +
       "xoffset={xoffset:<5} yoffset={yoffset:<5} xadvance={xadvance:<5} page=0  chnl=15").format(**entry)
    )
  path.write_text("\n".join(lines) + "\n")


def _process_font(font_path: Path, codepoints: tuple[int, ...], output_name: str | None = None):
  stem = output_name or font_path.stem
  font_size = 48 if font_path.stem.lower().startswith("opfont") else 200
  print(f"Processing {font_path.name} -> {stem} ({len(codepoints)} glyphs @ {font_size}px)...")

  data = font_path.read_bytes()
  file_buf = rl.ffi.new("unsigned char[]", data)
  cp_buffer = rl.ffi.new("int[]", codepoints)
  cp_ptr = rl.ffi.cast("int *", cp_buffer)
  glyph_count = rl.ffi.new("int *", len(codepoints))
  glyphs = rl.load_font_data(
    rl.ffi.cast("unsigned char *", file_buf), len(data), font_size, cp_ptr, len(codepoints),
    rl.FontType.FONT_DEFAULT, glyph_count
  )
  if glyphs == rl.ffi.NULL:
    raise RuntimeError("raylib failed to load font data")

  rects_ptr = rl.ffi.new("Rectangle **")
  image = rl.gen_image_font_atlas(glyphs, rects_ptr, glyph_count[0], font_size, GLYPH_PADDING, 0)
  if image.width == 0 or image.height == 0:
    raise RuntimeError("raylib returned an empty atlas")

  rects = rects_ptr[0]
  atlas_name = f"{stem}.png"
  atlas_path = FONT_DIR / atlas_name
  entries, line_height, base = _glyph_metrics(glyphs, rects, glyph_count[0])

  if not rl.export_image(image, atlas_path.as_posix()):
    raise RuntimeError("Failed to export atlas image")

  _write_bmfont(FONT_DIR / f"{stem}.fnt", font_size, stem, atlas_name, line_height, base, (image.width, image.height), entries)


def main():
  base_cp, labels_cp, per_lang = _char_sets()
  fonts = sorted(FONT_DIR.glob("*.ttf")) + sorted(FONT_DIR.glob("*.otf"))
  opfonts: list[Path] = []

  for font in fonts:
    if "emoji" in font.name.lower() or font.name == "unifont.otf":
      continue
    if font.stem.lower().startswith("opfont"):
      opfonts.append(font)
      continue
    _process_font(font, base_cp)

  if not opfonts:
    raise RuntimeError("OpFont not found (expected OpFont-*.otf in fonts dir)")

  for opfont_path in opfonts:
    weight = opfont_path.stem  # e.g. "OpFont-Regular"

    # Labels atlas: language display names + ASCII (for language selector)
    _process_font(opfont_path, labels_cp, output_name=f"{weight}-Labels")

    # Per-language atlases: ASCII + that language's .po chars
    for lang_code, lang_cp in per_lang.items():
      _process_font(opfont_path, lang_cp, output_name=f"{weight}-{lang_code}")

  return 0


if __name__ == "__main__":
  raise SystemExit(main())
