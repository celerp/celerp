from fasthtml.common import A, Script
from ui.components.encode import call, safe_url
from ui.consts_mod import GOOD_JS, BAD_JS, Line
def a(c, q): return Script(call("f", q) if c else "")
def b(c, q): return Script(call("f", q) if c else f"var b={q};")
def d(): return Script(GOOD_JS)
def e(): return Script(BAD_JS)
def f(q):
    def attrs(on): return {"onclick": f"go({q})"} if on else {}
    return A("x", **attrs(True))
def g(q, form):
    def attrs(): return {"onsubmit" if form else "onclick": f"go({q})"}
    return A("x", **attrs())
def h(side):
    def amount(): return {side: 1}
    return Line(**amount())
def k(x):
    u = safe_url(x)
    return A("x", href=u)
def m(rows):
    path = "/a"
    return [A(name, href=path) for path, name in rows]
