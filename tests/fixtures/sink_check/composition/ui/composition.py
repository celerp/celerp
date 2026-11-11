from fasthtml.common import Script
from ui.components.encode import js
from ui.consts_mod import GOOD_JS, BAD_JS
def a(c, q): return Script(js("var a={0};", q) if c else js(""))
def b(c, q): return Script(js("var a={0};", q) if c else f"var b={q};")
def d(): return Script(GOOD_JS)
def e(): return Script(BAD_JS)
