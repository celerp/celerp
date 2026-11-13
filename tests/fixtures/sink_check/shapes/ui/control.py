from fasthtml.common import Div, A, Script, NotStr
from starlette.responses import HTMLResponse
def c1(q): return A("x", href=q)
def c2(q): return Div("x", onclick=q)
def c3(q): return Script(f"var a='{q}';")
def c4(q): return HTMLResponse(f"<b>{q}</b>")
def c5(q): return Div(NotStr(q))
