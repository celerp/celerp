from starlette.responses import Response, PlainTextResponse
def a(v):
    r = PlainTextResponse(v)
    r.headers["content-type"] = "text/html"
    return r
def b(v):
    r = Response("x", media_type="text/plain")
    r.body = v.encode()
    r.headers.update({"content-type": "text/html"})
    return r
