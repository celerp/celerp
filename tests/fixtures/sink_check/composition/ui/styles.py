from fasthtml.common import Span
from ui.components.encode import css_color
def a(v):
    c = css_color(v) if v else None
    return Span(style=f"background:{c}" if c else "")
def b(v): return Span(style=f"background:{v}" if v else "background:#ccc")
def d(n): return Span(style=f"width:{int(n) if n else 0}px")
