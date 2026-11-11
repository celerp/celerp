# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The encoder module: the one place a value may become raw markup, script or a URL.

FastHTML components encode element text and attribute values. A value reaches a page
unencoded only through a raw door: markup admitted as already safe, a script or style
position, a URL attribute, or a browser sink. Every function that opens such a door lives
here, and each one encodes its holes for the position they sit in or refuses the position.
``scripts/sink_check.py`` refuses the raw primitives everywhere else and loads
``hole_positions`` from this file, so the run-time refusal and the static check share one
parser.
"""
from __future__ import annotations

import html
import json
import re
import string
from urllib.parse import urlsplit

from fastcore.xml import to_xml
from starlette.responses import HTMLResponse

from celerp.services.app_paths import is_app_local_path

# ---------------------------------------------------------------- template positions
HOLE = "\u0000HOLE\u0000"
EVENT_ATTR_RE = re.compile(r"^(on[a-z]+|hx_on\w*|hx-on\S*)$")
URL_ATTRS = {"href", "src", "action", "formaction", "srcset", "poster", "data", "xlink:href", "xlink_href"}
HTMX_JS_ATTRS = {"hx-vals", "hx-headers", "hx-trigger", "hx_vals", "hx_headers", "hx_trigger"}
REFUSED_ATTR = URL_ATTRS | HTMX_JS_ATTRS | {"srcdoc", "style"}
TAG_RE = re.compile(r"<(/?)([A-Za-z][\w:-]*)")
ATTR_RE = re.compile(r"""\s*([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]*)))?""")
TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)|(?P<lc>//[^\n]*)|(?P<bc>/\*.*?\*/)|"""
    r"""(?P<str>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)|"""
    r"""(?P<id>[A-Za-z_$\u0000][\w$\u0000A-Z]*)|(?P<num>\d[\w.]*)|"""
    r"""(?P<op>===|!==|==|!=|=>|\+=|-=|\|\|=|&&=|\?\?=|[=(){}\[\];,.:+\-*/%<>!?&|^~])|(?P<other>.)""",
    re.S)


def _holes(template: str) -> str:
    """The template with every str.format field replaced by HOLE ({{ and }} stay literal)."""
    out = []
    for lit, field, _spec, _conv in string.Formatter().parse(template):
        out.append(lit)
        if field is not None:
            out.append(HOLE)
    return "".join(out)


def hole_positions(template: str, kind: str) -> list[str]:
    """Refused hole positions in a raw_html ("html") or js ("js") template; [] = every hole is allowed.

    html: a hole may sit in element text or a quoted value of an ordinary attribute.
    js: a hole may sit only where a whole JavaScript expression goes (js_value output).
    A template ``str.format`` cannot parse (a lone brace) is refused as malformed.
    """
    try:
        s = _holes(template)
    except ValueError:
        return ["malformed template"]
    bad: list[str] = []
    if kind == "js":
        for m in TOKEN_RE.finditer(s):
            if HOLE in m.group() and m.lastgroup in ("str", "lc", "bc"):
                bad.append("JS string, template or comment")
        return bad
    i = 0
    while i < len(s):
        if s.startswith("<!--", i):
            j = s.find("-->", i)
            j = len(s) if j < 0 else j + 3
            if HOLE in s[i:j]:
                bad.append("comment")
            i = j
            continue
        m = TAG_RE.match(s, i)
        if not m:
            i += 1
            continue
        name = m.group(2).lower()
        if HOLE in m.group(2):
            bad.append("tag name")
        i = m.end()
        while i < len(s) and s[i] != ">":
            a = ATTR_RE.match(s, i)
            if not a or a.end() == i:
                if HOLE in s[i:i + len(HOLE)]:
                    bad.append("attribute name")
                    i += len(HOLE)
                else:
                    i += 1
                continue
            attr = a.group(1).lower()
            quoted = a.group(2) if a.group(2) is not None else a.group(3)
            if HOLE in a.group(1):
                bad.append("attribute name")
            elif a.group(4) and HOLE in a.group(4):
                bad.append(f"unquoted {attr} value")
            elif quoted and HOLE in quoted and (attr in REFUSED_ATTR or EVENT_ATTR_RE.match(attr)):
                bad.append(f"{attr} attribute")
            i = a.end()
        i += 1
        if name in ("script", "style") and not m.group(1):
            j = s.lower().find(f"</{name}", i)
            j = len(s) if j < 0 else j
            if HOLE in s[i:j]:
                bad.append(f"<{name}> text")
            i = j
    return bad


def _refuse(template: str, kind: str) -> None:
    bad = hole_positions(template, kind)
    if bad:
        raise ValueError(f"refused {kind} template hole: {', '.join(bad)}")


# ---------------------------------------------------------------- script
class Js(str):
    """Script text built by ``js``: every hole in it is one ``js_value`` literal."""

    def __add__(self, other):
        """Two scripts joined stay script; anything else joined is a plain string."""
        joined = str.__add__(self, other)
        return Js(joined) if isinstance(other, Js) else joined


def js_value(value) -> str:
    """*value* as one JavaScript literal that is safe inside a <script> element or a handler."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def js(template: str, *values) -> Js:
    """Script from a constant ``str.format`` template whose holes are whole expressions.

    Each hole becomes ``js_value(value)``; a hole inside a JavaScript string, template
    literal or comment raises ``ValueError``, since a literal there could close it.
    """
    _refuse(template, "js")
    return Js(template.format(*(js_value(v) for v in values)))


def as_js(x) -> Js:
    """*x* when it was built by ``js``; a plain string raises ``TypeError``."""
    if not isinstance(x, Js):
        raise TypeError("script must be built with js()")
    return x


# ---------------------------------------------------------------- URLs
_SAFE_SCHEMES = {"http", "https", "mailto", "tel"}


def safe_url(v) -> str:
    """*v* when it is an in-app path, a ``?`` or ``#`` reference, or an http(s), mailto or tel URL;
    otherwise ``"#"``. Never raises: a value that cannot be parsed goes nowhere.

    ``celerpSafeUrl`` in ``ui/static/celerp-dom.js`` mirrors this rule; this one is authoritative.
    """
    if not isinstance(v, str) or not v or any(ord(c) < 0x20 or ord(c) == 0x7F for c in v):
        return "#"
    if is_app_local_path(v) or v[0] in "?#":
        return v
    try:
        parts = urlsplit(v)
    except ValueError:
        return "#"
    if parts.scheme not in _SAFE_SCHEMES or "[" in parts.netloc or "]" in parts.netloc:
        return "#"
    return v


# ---------------------------------------------------------------- markup
class Admitted(str):
    """Markup built by this module: every hole in it was encoded for its position.

    FastHTML writes a child with ``__html__`` unescaped. Joining an ``Admitted`` with
    another string gives a plain ``str``, which FastHTML escapes again, so admission
    never spreads to text added later.
    """

    def __html__(self) -> str:
        return str.__str__(self)


def esc_attr(value: object) -> str:
    """HTML-escape a value for element text or a quoted attribute (quotes too)."""
    return html.escape(str(value), quote=True)


def raw_html(template: str, **holes) -> Admitted:
    """Markup from a constant ``str.format`` template with named holes.

    Each hole is escaped once with ``esc_attr`` unless it is already ``Admitted``. A hole
    that HTML escaping does not make safe (a URL, event, style, srcdoc or htmx-evaluated
    attribute, an attribute name, an unquoted value, a comment, script or style text)
    raises ``ValueError``.
    """
    _refuse(template, "html")
    return Admitted(template.format(**{k: v if isinstance(v, Admitted) else esc_attr(v)
                                       for k, v in holes.items()}))


def to_markup(node) -> Admitted:
    """A FastHTML node rendered to markup; a ``str`` or ``bytes`` raises ``TypeError``."""
    if isinstance(node, Admitted):
        return node
    if isinstance(node, (str, bytes)):
        raise TypeError("only a FastHTML node renders to markup")
    return Admitted(to_xml(node))


def html_response(node, **kwargs) -> HTMLResponse:
    """The one HTML response: a FastHTML node rendered by ``to_markup``."""
    return HTMLResponse(str(to_markup(node)), **kwargs)


_MARKUP_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml"}


def inert_media_type(media_type) -> str:
    """A stored file's content type, with HTML, XHTML, SVG and XML served as a download type."""
    if not isinstance(media_type, str):
        return "application/octet-stream"
    base = media_type.split(";", 1)[0].strip().lower()
    if base in _MARKUP_TYPES or base.endswith("/xml") or base.endswith("+xml"):
        return "application/octet-stream"
    return media_type


# ---------------------------------------------------------------- library markup
def _number(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError("SVG sizes must be numbers")
    return v


def _svg_root(inner: str, vb_w: float, vb_h: float, w_mm: float, h_mm: float, stretch: bool) -> str:
    """Wrap barcode/QR rects in an SVG root element.

    stretch=True fills the parent box (print sheet inlines the SVG in a sized
    div); otherwise the SVG carries its intrinsic mm size (designer preview
    loads it via <img>). preserveAspectRatio="none" lets both stretch freely,
    and shape-rendering="crispEdges" suppresses anti-aliased module edges.
    """
    size = 'width="100%" height="100%"' if stretch else f'width="{w_mm:.2f}mm" height="{h_mm:.2f}mm"'
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {vb_w:g} {vb_h:g}" '
        f'{size} preserveAspectRatio="none" shape-rendering="crispEdges">'
        f'<rect width="100%" height="100%" fill="#fff"/><g fill="#000">{inner}</g></svg>'
    )


def svg_bars(pattern: str, module_mm: float, shrink_mm: float, quiet_mm: float,
             min_bar_mm: float, height_mm: float, stretch: bool = False) -> str:
    """Barcode SVG from a bit pattern of "0" and "1" and sizes in mm.

    Each run of "1" is one bar, shaved on its trailing edge by *shrink_mm* but never
    narrower than *min_bar_mm*. A dot-addressed printer can only drop whole dots, so the
    caller passes sizes already snapped to its dot grid and every bar edge stays on it.
    """
    if not isinstance(pattern, str) or pattern.strip("01"):
        raise TypeError("a barcode pattern is a string of 0 and 1")
    module_mm, shrink_mm, quiet_mm, min_bar_mm, height_mm = (
        _number(v) for v in (module_mm, shrink_mm, quiet_mm, min_bar_mm, height_mm))
    bars = []
    i = 0
    while i < len(pattern):
        if pattern[i] == "1":
            j = i
            while j < len(pattern) and pattern[j] == "1":
                j += 1
            bw = max(min_bar_mm, (j - i) * module_mm - shrink_mm)
            bars.append(f'<rect x="{quiet_mm + i * module_mm:.4f}" y="0" width="{bw:.4f}" height="{height_mm:g}"/>')
            i = j
        else:
            i += 1
    w_mm = len(pattern) * module_mm + 2 * quiet_mm
    return _svg_root("".join(bars), w_mm, height_mm, w_mm, height_mm, stretch)


def svg_cells(matrix, size_mm: float, stretch: bool = False) -> str:
    """QR SVG from a matrix of booleans, drawn as a *size_mm* square."""
    size_mm = _number(size_mm)
    n = len(matrix)
    cells = []
    for r, row in enumerate(matrix):
        if any(not isinstance(c, (bool, int)) for c in row):
            raise TypeError("a QR matrix holds booleans")
        c0 = 0
        while c0 < n:
            if row[c0]:
                c1 = c0
                while c1 < n and row[c1]:
                    c1 += 1
                cells.append(f'<rect x="{c0}" y="{r}" width="{c1 - c0}" height="1"/>')
                c0 = c1
            else:
                c0 += 1
    return _svg_root("".join(cells), n, n, size_mm, size_mm, stretch)


LIBRARY_PRODUCERS = (svg_bars, svg_cells)


def library_markup(producer, *args) -> Admitted:
    """Markup from one of ``LIBRARY_PRODUCERS``, which format only numbers; any other producer raises."""
    if not any(producer is p for p in LIBRARY_PRODUCERS):
        raise ValueError("library markup comes only from LIBRARY_PRODUCERS")
    return Admitted(producer(*args))
