from ui.components.markup import raw_html
from ui.components.encode import js, js_value
from fasthtml.common import Div, Script, Button
def a1(u): return raw_html('<a href="{u}">open</a>', u=u)                 # URL attribute hole
def a2(n): return raw_html('<button onclick="go(\'{n}\')">x</button>', n=n) # handler hole
def a3(n): return raw_html('<script>var N="{n}";</script>', n=n)          # script text hole
def a4(n): return raw_html('<div hx-on::after-request="f(\'{n}\')"></div>', n=n)
def a5(s): return raw_html('<iframe srcdoc="{s}"></iframe>', s=s)
def a6(c): return raw_html('<div style="{c}"></div>', c=c)
def b1(v): return Script(js("go('{}');", v))                               # js hole inside a quoted literal
def b2(v): return Button("x", onclick=js("go('{}')", v))
def b3(v): return Script(js("var s = `{}`;", v))
