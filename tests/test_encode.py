# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The encoder module: every value placed in a page position is encoded for that position.

`ui.components.encode` is the one module that turns values into raw markup or script.
These tests hold each encoder to its position: a script is one call of a constant name with
one JSON literal per value, raw markup takes no values at all, URLs pass only the safe
schemes, stored file types are never served as markup, and library markup is admitted only
from the listed producers.
"""
from __future__ import annotations

import json
import random
import re

import pytest

from encode_values import hostile_values
from ui.components.encode import (
    EVENT,
    THIS,
    Admitted,
    as_js,
    call,
    css_color,
    inert_media_type,
    js_value,
    library_markup,
    raw_html,
    safe_url,
    svg_bars,
    svg_cells,
)

# Shared with the browser twin (test_url_rule_same_in_python_and_browser): value -> safe_url result.
URL_VECTORS = {
    "javascript:alert(1)": "#",
    "JavaScript:alert(1)": "#",
    " javascript:alert(1)": "#",
    "java\tscript:alert(1)": "#",
    "vbscript:msgbox(1)": "#",
    "data:text/html,<script>alert(1)</script>": "#",
    "//evil.example.com/x": "#",
    "/\\evil.example.com": "#",
    "http://[x": "#",
    "[": "#",
    "": "#",
    "ftp://example.com/f": "#",
    "/inventory?q=1": "/inventory?q=1",
    "?page=2": "?page=2",
    "#notes": "#notes",
    "https://example.com/a?b=1": "https://example.com/a?b=1",
    "http://example.com": "http://example.com",
    "mailto:someone@example.com": "mailto:someone@example.com",
    "tel:+6620000000": "tel:+6620000000",
}


def test_encoders_encode_hostile_values():
    hostile = "</script><script>alert('x')</script>\"\u2028\u2029"
    literal = js_value(hostile)
    assert not set(literal) & set("<>&\u2028\u2029")
    assert json.loads(literal) == hostile, "one JavaScript literal holding the whole value"
    assert js_value({"a": hostile}).startswith("{")

    with pytest.raises(TypeError):
        as_js("alert(1)")
    assert as_js(call("go", 1)) == "go(1)"

    for value, expected in URL_VECTORS.items():
        assert safe_url(value) == expected, value
    assert safe_url("http\x01://example.com") == "#"

    for markup in ("text/html", "text/html; charset=utf-8", "TEXT/HTML", "image/svg+xml",
                   "application/xhtml+xml", "text/xml", "application/xml"):
        assert inert_media_type(markup) == "application/octet-stream", markup
    for plain in ("application/pdf", "image/png", "text/plain", "text/csv"):
        assert inert_media_type(plain) == plain


def test_safe_url_never_raises():
    for value in ("http://[x", "[", None, "x" * 1000 + "\x00\x1f" * 500, 42, b"http://example.com"):
        assert safe_url(value) == "#"


def test_css_color_keeps_only_a_hex_colour():
    for value in ("#6366f1", "#FFF", "#11223344"):
        assert css_color(value) == value
    for value in ("red;background:url(//x)", "#6366f1;color:red", "#12", "", None, 42, "#gggggg"):
        assert css_color(value) is None, value


@pytest.mark.parametrize("name", [
    "", "1f", "f()", "f(1);g", "a..b", "a.", ".a", "a b", "a-b", "a[0]", "f\n", "f;", "a.1",
    "</script>", "f//", "f/*", "`f`", "a\u2028b", None, 1, b"f"])
def test_call_refuses_anything_but_a_dotted_identifier(name):
    with pytest.raises(ValueError):
        call(name, 1)


def test_call_writes_one_call_with_one_literal_per_value():
    assert call("celerpGo") == "celerpGo()"
    assert call("htmx.ajax", "GET", "/x", {"swap": "outerHTML"}) == 'htmx.ajax("GET","/x",{"swap": "outerHTML"})'
    assert call("$_.a$", THIS, EVENT, None, True, 1.5) == "$_.a$(this,event,null,true,1.5)"
    assert call("f", "this", "event") == 'f("this","event")', "only the THIS and EVENT objects are references"


def _split_args(out: str, name: str) -> list:
    """The call's arguments, read by a JSON decoder: each must be exactly one literal."""
    assert out.startswith(name + "(") and out.endswith(")")
    inner, args, dec, i = out[len(name) + 1:-1], [], json.JSONDecoder(), 0
    while i < len(inner):
        value, i = dec.raw_decode(inner, i)
        args.append(value)
        assert i == len(inner) or inner[i] == ",", inner[i:]
        i += 1
    return args


def test_call_output_is_one_call_of_literals_for_any_value():
    """Seeded generative oracle: every output reads back as the name and exactly the values
    given, and holds no character that ends a script element, an HTML comment or a JS line."""
    rng = random.Random(4250)
    for _ in range(3000):
        values = [rng.choice(hostile_values(rng)) for _ in range(rng.randint(0, 4))]
        out = call("rec.call", *values)
        assert _split_args(out, "rec.call") == values, out
        assert not set(out) & set("<>&\r\n\u2028\u2029"), out


def test_raw_html_takes_one_constant_and_no_values():
    out = raw_html('<path d="M0 0"/>')
    assert isinstance(out, Admitted) and out == '<path d="M0 0"/>'
    with pytest.raises(TypeError):
        raw_html('<b title="{t}">x</b>', t="v")
    with pytest.raises(TypeError):
        raw_html(None)


def test_library_markup_admits_only_listed_producers():
    matrix = [[True, False], [False, True]]
    svg = library_markup(svg_cells, matrix, 10)
    assert isinstance(svg, Admitted)
    text = str(svg)
    assert text.startswith("<svg") and "<rect" in text
    words = set(re.findall(r"[A-Za-z][A-Za-z:-]*", text))
    assert words <= {"svg", "xmlns", "http:", "www", "w", "org", "viewBox", "width", "height",
                     "mm", "preserveAspectRatio", "none", "shape-rendering", "crispEdges",
                     "rect", "x", "y", "fill", "fff", "g"}, f"only numbers reach the SVG: {words}"
    bars = str(library_markup(svg_bars, "1011", 0.25, 0.0, 1.0, 0.125, 10.0))
    assert bars.startswith("<svg") and bars.count("<rect x=") == 2, "one rect per run of 1s, beside the background"
    with pytest.raises(TypeError):
        library_markup(svg_bars, "1<b>1", 0.25, 0.0, 1.0, 0.125, 10.0)

    with pytest.raises(ValueError):
        library_markup(lambda *a: "<svg/>", matrix, 10)
    with pytest.raises(ValueError):
        library_markup(str, "<svg/>")
    with pytest.raises(TypeError):
        library_markup(svg_cells, [["<script>", True]], 10)
    with pytest.raises(TypeError):
        library_markup(svg_cells, matrix, "10")
