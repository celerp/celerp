from ui.components.encode import call, js_value, raw_html
from fasthtml.common import Animate, Button, Div, Img, Input, Script
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
