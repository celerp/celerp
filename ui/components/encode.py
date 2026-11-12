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
# A template is a constant ``str.format`` string. Its check is an allow list: a hole is accepted
# only where the scan below proves the position, and any construct the scan does not know
# refuses the whole template. Each template must also end where it started (outside any tag,
# string or comment), so joining two checked templates never moves a hole into a new position.
HOLE = "\u0000HOLE\u0000"
EVENT_ATTR_RE = re.compile(r"^(on[a-z]+|hx_on\w*|hx-on\S*)$")
URL_ATTRS = {"href", "src", "action", "formaction", "srcset", "poster", "data", "xlink:href", "xlink_href"}
HTMX_JS_ATTRS = {"hx-vals", "hx-headers", "hx-trigger", "hx_vals", "hx_headers", "hx_trigger"}
# Attributes whose value HTML escaping does not make safe: URLs (including the legacy and SVG
# animation ones), handlers, every htmx attribute (they fetch, swap or evaluate), styles,
# embedded documents and meta refresh.
REFUSED_ATTR = URL_ATTRS | HTMX_JS_ATTRS | {
    "srcdoc", "style", "background", "ping", "cite", "codebase", "longdesc", "manifest", "lowsrc",
    "dynsrc", "archive", "classid", "usemap", "profile", "icon", "content", "http-equiv",
    "values", "to", "from", "by", "attributename"}
REFUSED_ATTR_RE = re.compile(r"^(on|hx-|hx_|data-hx-|xlink:|xml:)")
# Elements whose content the browser does not tokenize as markup (or tokenizes differently
# inside SVG and MathML): the scan cannot follow them, so a raw_html template may not open one.
RAW_TEXT_TAGS = {"script", "style", "xmp", "iframe", "noembed", "noframes", "noscript", "plaintext",
                 "textarea", "title"}
_WS = " \t\n\f\r"
_TAG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9:-]*")
_ATTR_NAME = re.compile(r"[A-Za-z_:][-A-Za-z0-9_:.]*")
_UNQUOTED = re.compile(r"[^\s\"'=<>`\u0000]+")
_FIELD = {"js": re.compile(r"\d*"), "html": re.compile(r"[A-Za-z_]\w*")}
TOKEN_RE = re.compile(
    r"""(?P<ws>\s+)|(?P<lc>//[^\n]*)|(?P<bc>/\*.*?\*/)|"""
    r"""(?P<str>'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)|"""
    r"""(?P<id>[A-Za-z_$\u0000][\w$\u0000A-Z]*)|(?P<num>\d[\w.]*)|"""
    r"""(?P<op>===|!==|==|!=|=>|\+=|-=|\|\|=|&&=|\?\?=|[=(){}\[\];,.:+\-*/%<>!?&|^~])|(?P<other>.)""",
    re.S)
JS_BEFORE_HOLE = {"(", ",", "=", ":", "[", "?", "return", None}  # None: the template's start
JS_AFTER_HOLE = {")", ",", ";", "]", "}", ":", None}  # None: the template's end


def _holes(template: str, kind: str) -> str:
    """The template with every str.format field replaced by HOLE ({{ and }} stay literal).

    A field is only a bare name (html) or index (js): an attribute, index, conversion or
    format spec would reshape the encoded value after encoding, so it raises ValueError.
    """
    if "\u0000" in template:
        raise ValueError
    out = []
    for lit, field, spec, conv in string.Formatter().parse(template):
        out.append(lit)
        if field is not None:
            if spec or conv or not _FIELD[kind].fullmatch(field):
                raise ValueError
            out.append(HOLE)
    return "".join(out)


def _js_refusals(s: str) -> list[str]:
    bad: list[str] = []
    if "<!--" in s or "-->" in s:
        bad.append("HTML comment marker")
    toks: list[str] = []
    for m in TOKEN_RE.finditer(s):
        kind, text = m.lastgroup, m.group()
        if kind == "ws":
            continue
        if kind in ("lc", "bc"):
            if HOLE in text:
                bad.append("hole in a comment")
            if kind == "lc" and m.end() == len(s):
                bad.append("line comment at the template's end")
            continue
        if kind == "str":
            if HOLE in text:
                bad.append("hole in a string")
            if text[0] == "`":
                bad.append("template literal")
            continue
        if text == "/":
            bad.append("'/' outside a string or comment")
        elif kind == "other":
            bad.append(f"unknown token {text!r}")
        elif HOLE in text and text != HOLE:
            bad.append("hole joined to another token")
        toks.append(text)
    for i, text in enumerate(toks):
        if text == HOLE:
            before = toks[i - 1] if i else None
            after = toks[i + 1] if i + 1 < len(toks) else None
            if before not in JS_BEFORE_HOLE or after not in JS_AFTER_HOLE:
                bad.append(f"hole not a whole expression ({before or 'start'} {{}} {after or 'end'})")
    return bad


def _html_scan(s: str) -> tuple[list[str], list[str]]:
    """(refusals, the position of each hole in order: "text" or "attr")."""
    bad: list[str] = []
    where: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s.startswith(HOLE, i):
            where.append("text")
            i += len(HOLE)
            continue
        if s[i] != "<":
            i += 1
            continue
        if s.startswith("<!--", i):
            j = s.find("-->", i + 4)
            body = s[i + 4:j]
            if j < 0 or body.startswith((">", "->")) or "--!>" in body:
                return bad + ["unterminated or malformed comment"], where
            if HOLE in body:
                bad.append("hole in a comment")
            i = j + 3
            continue
        closing = s.startswith("</", i)
        m = _TAG_NAME.match(s, i + 1 + closing)
        if not m or m.end() >= n or s[m.end()] not in _WS + "/>":
            return bad + ["'<' that does not open a constant tag"], where
        name = m.group().lower()
        i = m.end()
        if closing:
            while i < n and s[i] in _WS:
                i += 1
            if i >= n or s[i] != ">":
                return bad + ["malformed closing tag"], where
            i += 1
            continue
        if name in RAW_TEXT_TAGS:
            return bad + [f"<{name}> element"], where
        while True:
            while i < n and s[i] in _WS:
                i += 1
            if i >= n:
                return bad + ["unterminated tag"], where
            if s[i] == ">" or s.startswith("/>", i):
                i += 1 if s[i] == ">" else 2
                break
            a = _ATTR_NAME.match(s, i)
            if not a or a.end() >= n or s[a.end()] not in _WS + "/>=":
                return bad + ["attribute name"], where
            attr = a.group().lower()
            i = a.end()
            while i < n and s[i] in _WS:
                i += 1
            if i >= n or s[i] != "=":
                continue
            i += 1
            while i < n and s[i] in _WS:
                i += 1
            if i < n and s[i] in "\"'":
                j = s.find(s[i], i + 1)
                if j < 0:
                    return bad + ["unterminated attribute value"], where
                value, i = s[i + 1:j], j + 1
                if i < n and s[i] not in _WS + "/>":
                    return bad + ["attribute boundary"], where
                if HOLE in value:
                    if attr in REFUSED_ATTR or REFUSED_ATTR_RE.match(attr):
                        bad.append(f"{attr} attribute")
                    where.extend(["attr"] * value.count(HOLE))
                continue
            u = _UNQUOTED.match(s, i)
            if not u:
                return bad + ["attribute value"], where
            if HOLE in u.group():
                bad.append(f"unquoted {attr} value")
            i = u.end()
    return bad, where


def hole_positions(template: str, kind: str) -> list[str]:
    """Why a raw_html ("html") or js ("js") template is refused; [] = every hole is allowed.

    html: a hole may sit only in element text or in a quoted value of an attribute outside
    ``REFUSED_ATTR``; every "<" must open a constant tag, a closing tag or a comment, and the
    template may not open a raw-text element or end inside a tag, value or comment.
    js: a hole may sit only where a whole expression goes, between the tokens of
    ``JS_BEFORE_HOLE`` and ``JS_AFTER_HOLE``; the template may hold no "/" or backtick outside
    a string or comment (no regular expression or template literal can wrap a hole), no
    unterminated string and no line comment at its end.
    A template ``str.format`` cannot parse, or a field that is not a bare name or index, is
    refused as malformed.
    """
    try:
        s = _holes(template, kind)
    except ValueError:
        return ["malformed template"]
    return _js_refusals(s) if kind == "js" else _html_scan(s)[0]


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
    return (json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


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


# ---------------------------------------------------------------- styles
_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}")


def css_color(v) -> str | None:
    """*v* when it is a hex colour (what a colour input submits), otherwise None, so a stored
    value can never add its own declarations to a style attribute."""
    return v if isinstance(v, str) and _HEX_COLOR.fullmatch(v) else None


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
    in_attr = {field for (_lit, field, _s, _c), at in zip(
        (p for p in string.Formatter().parse(template) if p[1] is not None),
        _html_scan(_holes(template, "html"))[1]) if at == "attr"}
    return Admitted(template.format(**{
        k: v if isinstance(v, Admitted) and k not in in_attr else esc_attr(v) for k, v in holes.items()}))


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
