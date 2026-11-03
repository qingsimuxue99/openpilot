"""Chinese text must not render with a Latin-only font.

The onroad navigation box went through draw_text_ui_style, which draws with the native
CFFI path instead of rl.draw_text_ex, so the glyph fallback patch never ran and Chinese
came out garbled (Pretendard/Inter carry no CJK glyphs at all).
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import typing

LIB = Path(__file__).parents[3] / 'system/ui/lib'


def _extract(filename, names, scope):
  tree = ast.parse((LIB / filename).read_text(encoding='utf8'))
  body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
  exec(compile(ast.Module(body=body, type_ignores=[]), filename, 'exec'), scope)
  return scope


def _font(*codepoints):
  return SimpleNamespace(glyphCount=len(codepoints),
                         glyphs=[SimpleNamespace(value=value) for value in codepoints])


def _fallback_scope(glyphs):
  scope = {'rl': SimpleNamespace(Font=object, ffi=SimpleNamespace(new=lambda *a: [], cast=lambda *a: None,
                                                                 sizeof=lambda *a: 0)),
           'Optional': typing.Optional, 'FONT_DIR': Path('.'), '_CJK_SOURCE_FONT': 'x.ttf',
           '_CJK_FONT_SIZE': 48, '_font_cmap_cache': {},
           '_cjk_extra': {'chars': set(), 'font': None},
           'cloudlog': SimpleNamespace(exception=lambda *a, **k: None)}
  scope['_cjk_font_with'] = lambda codepoints: glyphs.setdefault('built', object())
  return _extract('application.py', ['_font_cmap', 'font_with_glyphs'], scope)


def test_ascii_text_keeps_its_font_without_touching_the_cmap():
  built = {}
  scope = _fallback_scope(built)
  font = _font(ord('A'))

  assert scope['font_with_glyphs'](font, 'SET 65 km/h') is font
  assert 'built' not in built  # the hot path must not build a font atlas


def test_text_the_font_already_covers_keeps_its_font():
  built = {}
  scope = _fallback_scope(built)
  font = _font(ord('A'), ord('한'), ord('글'))

  assert scope['font_with_glyphs'](font, '한글') is font
  assert 'built' not in built


def test_chinese_switches_to_the_fallback_font():
  built = {}
  scope = _fallback_scope(built)
  font = _font(ord('A'), ord('한'))

  picked = scope['font_with_glyphs'](font, '前方路口右转')
  assert picked is built['built']
  assert picked is not font


def test_navigation_text_is_drawn_with_the_fallback_font(monkeypatch):
  """draw_text_ui_style must pick the glyph-complete font before the native draw."""
  drawn = []
  scope = {
    'rl': SimpleNamespace(Color=lambda *a, **k: 'color', BLACK='black', WHITE='white', Vector2=lambda x, y: (x, y)),
    'FONT_SCALE': 1.0, 'FontWeight': SimpleNamespace(DISPLAY='display'),
    'gui_app': SimpleNamespace(font=lambda weight: 'base_font'),
    'font_fallback': lambda font, text: 'display_font',
    'font_with_glyphs': lambda font, text: f'glyph_font({text})',
    'measure_text_cached': lambda font, text, size: SimpleNamespace(x=len(text) * size, y=size),
    'get_text_draw_pos': lambda font, text, x, y, size, align, y_offset: (x, y, None),
    'native_text': SimpleNamespace(try_text=lambda *args: drawn.append(args) or True),
    '_RAW_DRAW_TEXT_EX': object(),
    '_OUTLINE_UNIT_OFFSETS': (),
    'math': __import__('math'),
  }
  _extract('text_draw.py', ['draw_text_ui_style'], scope)

  scope['draw_text_ui_style']('前方路口右转', 10, 20, 40, 'white', align='left_bottom')

  assert drawn, 'native path should have drawn the text'
  assert drawn[0][1] == 'glyph_font(前方路口右转)'  # second argument of native_text.try_text is the font
