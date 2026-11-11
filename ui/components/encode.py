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

import json
import re
import string
from urllib.parse import urlsplit

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
    """
    s = _holes(template)
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
            if HOLE in attr:
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
