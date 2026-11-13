from ui.components.encode import call, js_value, raw_html
from fasthtml.common import Animate, Button, Div, Img, Input, Meta, Script
def s1(v): return raw_html("<{t}" + ">x", t=v)                         # holed template built by +
def s2(c, v): return raw_html("<b>{x}</b>" if c else "<{x}>", x=v)     # holed template in a condition
def s3(v): return Script(js("var r = /" + "{0}/;", v))                 # script template built by +
def s4(v): return raw_html(f"<p>{v}</p>")                              # built markup
def s5(name, v): return Script(call(name, v))                          # computed function name
def s6(v): return Button("x", onclick=call("f(1);g", v))               # name that is not an identifier
def s7(v): return Script(f"go({js_value(v)});")                        # value written into script text
def s8(c, v): return Script("a();" if c else v)                        # one branch is a value
def p1(v): return Animate(values=v)                                    # SVG animation attribute
def p2(v): return Div(foo=v)                                           # name off the allow list
def p3(v): return Div(id=v)                                            # id with no prefix
def p4(v): return Img(name=v)                                          # name the document exposes
def ok(c, v):
    return (Div(id=f"row-{v}"), Input(name=v), Div(data_x=v), Div(title=v), Script(call("f", v)),
            Button("x", onclick=call("celerp.go", v)), Script("a();" if c else "b();"))
def p5(row): return Div(id=row["id"])                                  # id from a computed value
def p6(v): return Div(id=v)                                            # id parameter given through a keyword splat
def p6c(kw): return p6(**kw)
def p7(v): return Div(id=v)                                            # id parameter given a computed value
def p7c(row): return p7(row["id"])
def u1(v): return Meta(http_equiv="refresh", content=f"{v}")           # refresh URL that is only a value
def s9(v): return Script(call(f"{GO!r}", v))                           # name through a conversion
TOP = Div(id=__name__)                                                 # id outside any function, no prefix
def s10(a):
    a = a + "a();"
    return Script(a), Div(id=a)                                        # a parameter rebound to itself
GO = "go"
MODE = "a()" if __debug__ else "b()"
def ok2(v):
    return Script(MODE + ";"), Button("x", onclick=call("celerp." + "go", v))
class Rows:                                                            # a method id parameter given a prefixed value
    def cell(self, rid): return Div(id=rid)
    def page(self, v): return self.cell(f"row-{v}")
