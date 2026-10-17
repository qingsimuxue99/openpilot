#!/usr/bin/env python3
from pathlib import Path
import json

import pyray as rl

FONT_DIR = Path(__file__).resolve().parent
SELFDRIVE_DIR = FONT_DIR.parents[1]
TRANSLATIONS_DIR = SELFDRIVE_DIR / "ui" / "translations"
LANGUAGES_FILE = TRANSLATIONS_DIR / "languages.json"

GLYPH_PADDING = 6
# ⚠️ 「│」(U+2502) 用于顶部版本条分隔符，缺它会显示成 "?"（2026-10-17 实机踩过）
EXTRA_CHARS = "–‑✓×°§•X⚙✕◀▶✔⌫⇧␣○●↳çêüñ│─┃┌┐└┘–‑✓×°§•€£¥"
UNIFONT_LANGUAGES = {"th", "zh-CHT", "zh-CHS", "ko", "ja"}
KR_FONTS = {"KaiGenGothicKR-Bold"}

def _languages():
  if not LANGUAGES_FILE.exists():
    return {}
  with LANGUAGES_FILE.open(encoding="utf-8") as f:
    return json.load(f)


def _ui_source_chars():
  """收集设备 UI 里所有硬编码中文字符。

  为什么需要：``.po`` 翻译文件只覆盖走 tr() 的文案，而 carrot / 软件菜单等
  大量中文是直接写在 .py（software.py / carrot.py / home.py / sidebar.py …）
  与 ``carrot_settings.json`` 的 ctitle/cdescr 里的，不进 .po。若字库只按
  .po 烘焙，这些字在设备上就会显示成 "?"。
  """
  found = set()
  # 1) UI 相关源码：递归扫 selfdrive/ui 与 selfdrive/carrot 下的 .py
  for root in (SELFDRIVE_DIR / "ui", SELFDRIVE_DIR / "carrot"):
    if not root.exists():
      continue
    for path in root.rglob("*.py"):
      if "__pycache__" in path.parts or path.name == "process.py":
        continue
      try:
        found.update(path.read_text(encoding="utf-8", errors="ignore"))
      except OSError:
        continue

  # 2) 设置目录（含中文 ctitle/cdescr/ctgroup）
  for name in ("carrot_settings.json", "carrot_setting.py"):
    path = SELFDRIVE_DIR / name
    if path.exists():
      try:
        found.update(path.read_text(encoding="utf-8", errors="ignore"))
      except OSError:
        pass

  # 只保留 CJK 与常用中文标点，避免把代码符号塞进图集
  return {c for c in found if "一" <= c <= "鿿" or "　" <= c <= "〿" or "＀" <= c <= "￯"}


def _char_sets():
  base = set(map(chr, range(32, 127))) | set(EXTRA_CHARS)
  unifont = set(base)

  unifont.update(map(chr, range(0xAC00, 0xD7A4)))
  for language, code in _languages().items():
    unifont.update(language)
    po_path = TRANSLATIONS_DIR / f"app_{code}.po"
    try:
      chars = set(po_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
      continue
    (unifont if code in UNIFONT_LANGUAGES else base).update(chars)

  # 设备 UI 源码里硬编码的中文（不进 .po 的那些），必须并入否则显示 "?"
  ui_chars = _ui_source_chars()
  unifont.update(ui_chars)
  base.update(ui_chars)

  return tuple(sorted(ord(c) for c in base)), tuple(sorted(ord(c) for c in unifont))


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


def _process_font(font_path: Path, codepoints: tuple[int, ...]):
  print(f"Processing {font_path.name}...")

  #font_size = {
  #  "unifont.otf": 16,  # unifont is only 16x8 or 16x16 pixels per glyph
  #}.get(font_path.name, 200)
  font_size = 200
  padding = GLYPH_PADDING

  if font_path.name == "unifont.otf":
    font_size = 16
    padding = GLYPH_PADDING

  if font_path.stem in KR_FONTS:
    font_size = 48
    padding = 2
    
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
  image = rl.gen_image_font_atlas(glyphs, rects_ptr, glyph_count[0], font_size, padding, 0)
  if image.width == 0 or image.height == 0:
    raise RuntimeError("raylib returned an empty atlas")

  rects = rects_ptr[0]
  atlas_name = f"{font_path.stem}.png"
  atlas_path = FONT_DIR / atlas_name
  entries, line_height, base = _glyph_metrics(glyphs, rects, glyph_count[0])

  if not rl.export_image(image, atlas_path.as_posix()):
    raise RuntimeError("Failed to export atlas image")

  _write_bmfont(FONT_DIR / f"{font_path.stem}.fnt", font_size, font_path.stem, atlas_name, line_height, base, (image.width, image.height), entries)


def main():
  base_cp, unifont_cp = _char_sets()
  fonts = sorted(FONT_DIR.glob("*.ttf")) + sorted(FONT_DIR.glob("*.otf"))
  for font in fonts:
    if "emoji" in font.name.lower():
      continue
    glyphs = unifont_cp if font.stem.lower().startswith("unifont") or font.stem in KR_FONTS else base_cp
    _process_font(font, glyphs)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
