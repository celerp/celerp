from fasthtml.common import Div, A, Td, Meta, Button, to_xml, ft_hx, Script
from fasthtml.components import ft_html
from fastcore.xml import ft
from starlette.responses import HTMLResponse, Response, FileResponse

def r01_route_returns_fstring(q: str):
    return f"<b>{q}</b>"                       # FastHTML _resp: str -> HTMLResponse

def r02_htmlresponse_of_to_xml_str(q: str):
    return HTMLResponse(to_xml(q))             # to_xml(str) returns the str unescaped

def r03_child_to_xml_str(q: str):
    return Div(to_xml(q))                      # Safe child, never escaped

def r04_bytes_child(q: str):
    return Div(q.encode())                     # bytes child decoded raw

def r05_positional_dict_attr(q: str):
    return Div("x", {"onclick": q})            # Mapping child -> attributes

def r06_dict_from_data(row: dict):
    return Td("x", row["meta"])                # stored JSON dict -> attribute names unescaped

def r07_splat_computed_keys(k: str, v: str):
    return Div("x", **{k: v})                  # attribute name from data, unescaped

def r08_ft_setattr(q: str):
    el = Div("x"); el.onclick = q; return el   # FT.__setattr__ writes an attribute

def r09_ft_attrs_subscript(q: str):
    el = A("x"); el.attrs["href"] = q; return el

def r10_ft_call_kw(q: str):
    el = Div("x"); return el(onclick=q)

def r11_ft_set(q: str):
    return A("x").set(href=q)

def r12_ft_hx_script(q: str):
    return ft_hx("script", q)                  # lowercase tag builder

def r13_ft_script(q: str):
    return ft("script", q)

def r14_ft_html_a(q: str):
    return ft_html("a", "x", href=q)

def r15_meta_refresh(q: str):
    return Meta(http_equiv="refresh", content=f"0;url={q}")

def r16_hx_vals_js(q: str):
    return Div("x", hx_vals=f"js:{{a: {q}}}")   # htmx evaluates js: values

def r17_hx_trigger_filter(q: str):
    return Div("x", hx_trigger=f"click[{q}]")   # htmx evaluates trigger filters

def r18_hx_headers_js(q: str):
    return Div("x", hx_headers=f"js:{{a: {q}}}")

def r19_response_header_ct(q: str):
    return Response(q, headers={"content-type": "text/html"})

def r20_response_media_var(q: str, mt: str = "text/html"):
    return Response(q, media_type=mt)

def r21_alias_script(q: str):
    S = Script
    return S(q)

def r22_fileresponse_upload(path: str):
    return FileResponse(path)                  # uploaded .html/.svg served same-origin

def r23_button_formaction(q: str):
    return Button("x", formaction=q)

def r24_xlink(q: str):
    return ft_hx("a", "x", **{"xlink:href": q})
