from fasthtml.common import *
import fasthtml.common as fh
from importlib import import_module
def page(v, u, name):
    a = NotStr(v)                                   # PY-RAW
    b = fh.Safe(v)                                  # PY-RAW (attribute)
    c = getattr(fh, name)                           # PY-COMPUTED
    d = getattr(import_module("fasthtml.common"), name)  # PY-COMPUTED
    r = HTMLResponse(v)                             # PY-HTMLRESP
    s = Script(f"var x = {v};")                     # PY-JSHOLE
    s2 = Script(f"var x = {js_value(v)};")          # clean
    btn = Button("x", onclick=f"go('{v}')")         # PY-JSHOLE
    st = Style(f"a{{color:{v}}}")                   # PY-CSSHOLE
    l1 = A("x", href=u)                             # PY-URLHOLE
    l2 = A("x", href=f"/docs/{u}")                  # clean
    l3 = A("x", href=f"/{u}")                       # PY-URLHOLE (protocol-relative)
    l4 = A("x", href="javascript:alert(1)")         # JS-SCHEME
    k = "NotStr"                                    # PY-RAW (string)
    js = "el.innerHTML = v; document.write(v); el.insertAdjacentHTML('beforeend', v);"
    js2 = "var s=document.createElement('script'); eval(v); setTimeout('x()', 1); new Function(v); el.onclick='x()';"
    js3 = "window.location = v; location.href = v; location.assign(v); location.replace(v); window.open(v); a.href = v; f.action = v;"
    js4 = "el['inner'+'HTML'] = v; el['innerHTML'] = v; el.setAttribute(n, v); el.setAttribute('href', v); Object.assign(el, {href: v}); var o = {innerHTML: v};"
    js5 = "if (window.x) { setTimeout(function(){}, 1); setTimeout(() => 1, 2); }"
    t = to_xml(Div(v), do_escape=False)             # PY-RAW (unescaped serialisation)
    m1 = raw_html(f"<p>{v}</p>")                    # PY-RAW (built template)
    m2 = Script(js(v))                              # PY-JSHOLE (built template)
    m3 = Js(v)                                      # PY-RAW (Js outside encode.py)
    p1 = raw_html("<a href=\"{u}\">x</a>", u=v)        # PY-POSITION (URL attribute)
    p2 = js("go('{}');", v)                         # PY-POSITION (hole in a JS string)
    h1 = Response(v, media_type="text/html")        # PY-HTMLRESP
    h2 = PlainTextResponse(v); h2.body = v          # PY-HTMLRESP (write after construction)
    ok1 = Script(js("go({});", v))                  # clean: constant template
    ok2 = raw_html("<b>{x}</b>", x=v)               # clean: constant template
    return p1, p2, h1, h2, m1, m2, m3, ok1, ok2, t, a, b, c, d, r, s, s2, btn, st, l1, l2, l3, l4, k, js, js2, js3, js4, js5
