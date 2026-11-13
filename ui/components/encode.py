# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The encoder module: the one place a value may become raw markup, script or a URL.

FastHTML components encode element text and attribute values. A value reaches a page
unencoded only through a raw door: constant markup admitted as it is, a script, a style, a
URL attribute, or a browser sink. Every function that opens such a door lives here, and none
of them writes a value into markup or script source by template: a script with values is one
``call`` of a constant function name with JSON arguments, and markup with values is built as
FastHTML elements. ``scripts/sink_check.py`` refuses the raw primitives everywhere else.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

from fastcore.xml import to_xml
from starlette.responses import HTMLResponse

from celerp.services.app_paths import is_app_local_path

# ---------------------------------------------------------------- script
# A script never holds a value written into its source by a template. A handler or a one-line
# script is ``call(name, *values)``: one call of a constant function name with each value as one
# JSON literal, so its shape is fixed by construction and there is nothing to scan. A longer
# script is a constant; it reads its values from its own ``data-cfg`` attribute.
_JS_NAME = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_JS_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "\u2028": "\\u2028", "\u2029": "\\u2029"}


class Js(str):
    """Script built by ``call``."""


class JsRef(str):
    """A name ``call`` writes as itself: only ``THIS`` and ``EVENT``, the handler's element and event."""


THIS = JsRef("this")
EVENT = JsRef("event")


def js_value(value) -> str:
    """*value* as one JSON literal with no character that can end a script element, an HTML
    comment or a JavaScript line: ``<``, ``>``, ``&``, U+2028 and U+2029 are written as escapes
    (JSON already escapes every other control character)."""
    return "".join(_JS_ESCAPES.get(c, c) for c in json.dumps(value, ensure_ascii=False))


def call(name: str, *values) -> Js:
    """``name(v1, v2, ...)``: *name* is a constant dotted identifier and each value one
    ``js_value`` literal (``THIS`` and ``EVENT`` are written as themselves). Any other name
    raises ``ValueError``."""
    if not isinstance(name, str) or not _JS_NAME.fullmatch(name):
        raise ValueError(f"call() needs a function name, not {name!r}")
    args = ",".join(v if v is THIS or v is EVENT else js_value(v) for v in values)
    return Js(f"{name}({args})")


def as_js(x) -> Js:
    """*x* when it was built by ``call``; a plain string raises ``TypeError``."""
    if not isinstance(x, Js):
        raise TypeError("script must be built with call()")
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
    """Markup built by this module: a constant, a rendered FastHTML node or library markup.

    FastHTML writes a child with ``__html__`` unescaped. Joining an ``Admitted`` with
    another string gives a plain ``str``, which FastHTML escapes again, so admission
    never spreads to text added later.
    """

    def __html__(self) -> str:
        return str.__str__(self)


def raw_html(markup: str) -> Admitted:
    """A constant piece of markup (an icon's shapes, a fixed comment) admitted as it is.

    It takes no values: markup with a value in it is built as FastHTML elements, which encode
    their text and attributes. ``scripts/sink_check.py`` accepts only a plain string constant here.
    """
    if not isinstance(markup, str):
        raise TypeError("raw_html takes a constant string")
    return Admitted(markup)


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
