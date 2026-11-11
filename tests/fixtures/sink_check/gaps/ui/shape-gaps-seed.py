from fasthtml.common import *
from starlette.responses import Response
HTML = "text/html"
def a(v, u, s):
    r1 = Response(s, headers={"content-type": "text/html"})
    r2 = Response(content=s, media_type=HTML)
    r3 = Response(s, media_type="image/svg+xml")
    x4 = Object("x", data=u)
    x5 = ft("script", v)
    x6 = ft_hx("a", "t", href=u)
    x7 = Svg(A("x", xlink_href=u))
    x8 = Meta(http_equiv="refresh", content=f"0;url={u}")
    return r1, r2, r3
