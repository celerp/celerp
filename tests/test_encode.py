# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The encoder module: every value placed in a page position is encoded for that position.

`ui.components.encode` is the one module that turns values into raw markup or script.
These tests hold each encoder to its position: JavaScript literals stay one literal, raw
markup holes are escaped exactly once and refused where HTML escaping is not enough, URLs
pass only the safe schemes, stored file types are never served as markup, and library
markup is admitted only from the listed producers.
"""
from __future__ import annotations

import json
import re

import pytest

from encode_templates import (
    HOSTILE, NEUTRAL, REFUSED_SHAPES, generated_templates, render, repo_templates, shape)
from ui.components.encode import (
    Admitted,
    as_js,
    css_color,
    hole_positions,
    inert_media_type,
    js,
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
    hostile = "</script><script>alert('x')</script>\"  "
    literal = js_value(hostile)
    assert "</" not in literal and "<" not in literal
    assert json.loads(literal) == hostile, "one JavaScript literal holding the whole value"
    assert js_value({"a": hostile}).startswith("{")

    with pytest.raises(TypeError):
        as_js("alert(1)")
    assert as_js(js("go({});", 1)) == "go(1);"

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


def test_raw_html_escapes_holes_once():
    out = raw_html('<b title="{t}">{x}</b>', t='a"b', x="<i>&amp;</i>")
    assert isinstance(out, Admitted)
    assert str(out) == '<b title="a&quot;b">&lt;i&gt;&amp;amp;&lt;/i&gt;</b>'
    nested = raw_html("<p>{inner}</p>", inner=raw_html("<i>{v}</i>", v="<x>"))
    assert str(nested) == "<p><i>&lt;x&gt;</i></p>", "a nested raw_html is kept, encoded once"


@pytest.mark.parametrize("template", [
    '<a href="{v}">x</a>',
    '<img src="{v}">',
    '<form action="{v}"></form>',
    '<button formaction="{v}">x</button>',
    '<img srcset="{v}">',
    '<video poster="{v}"></video>',
    '<object data="{v}"></object>',
    '<use xlink:href="{v}"></use>',
    '<button onclick="go(\'{v}\')">x</button>',
    '<div hx-on:click="f(\'{v}\')"></div>',
    '<iframe srcdoc="{v}"></iframe>',
    '<div style="{v}"></div>',
    "<style>a{{color:{v}}}</style>",
    '<script>var n = "{v}";</script>',
    '<div hx-vals=\'{v}\'></div>',
    "<div {v}></div>",
    "<div title={v}></div>",
    "<!-- {v} -->",
])
def test_raw_html_refuses_holes_by_position(template):
    with pytest.raises(ValueError):
        raw_html(template, v="x")


@pytest.mark.parametrize("template", ['<p title="{v}">t</p>', '<p class="{v}">t</p>', "<p>{v}</p>"])
def test_raw_html_text_and_quoted_attribute_holes_render_escaped(template):
    assert "&lt;s&gt;&quot;" in str(raw_html(template, v='<s>"'))


@pytest.mark.parametrize("template", [
    "go('{}');",
    'go("{}");',
    "var s = `{}`;",
    "var s = `${{}}{}`;",
    "go(1); // {}",
    "go(1); /* {} */",
])
def test_js_refuses_holes_in_literals(template):
    with pytest.raises(ValueError):
        js(template, "x")


@pytest.mark.parametrize("template", ["x { y", "}", ".a { color: red } }"])
def test_malformed_templates_are_refused(template):
    assert hole_positions(template, "html") == ["malformed template"]
    with pytest.raises(ValueError):
        raw_html(template)
    with pytest.raises(ValueError):
        js(template)


def test_js_whole_expression_hole_is_one_literal():
    value = "');alert(1);//</script>"
    out = js("go({});", value)
    assert out == f"go({js_value(value)});"
    assert "</script>" not in out


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


@pytest.mark.parametrize("kind,template", REFUSED_SHAPES)
def test_hole_positions_refuses_holes_outside_the_allow_list(kind, template):
    assert hole_positions(template, kind)
    with pytest.raises(ValueError):
        render(kind, template, NEUTRAL)


@pytest.mark.parametrize("kind,template", [
    ("html", "<p>{v}</p>"),
    ("html", '<p title="{v}" class=\'{w}\'>{v}</p>'),
    ("html", '<a href="/x" title="{v}">{v}</a>'),
    ("html", "<p>a</p><!-- c --><br/>{v}"),
    ("js", "{0}"),
    ("js", "f({0}, {1});"),
    ("js", "var a = {0}, b = [{1}];"),
    ("js", "o = {{k: {0}}}; c ? {0} : {1};"),
    ("js", "function f() {{ return {0}; }}"),
    ("js", "f('/a/b', {0}); /* c/d */"),
])
def test_hole_positions_admits_allowed_shapes(kind, template):
    assert hole_positions(template, kind) == []


def test_js_value_escapes_line_separators():
    assert js_value("a\u2028b\u2029c") == '"a\\u2028b\\u2029c"'


_INVARIANCE = sorted(set(repo_templates()) | {(k, t) for k in ("html", "js") for t in generated_templates(k)})


def test_invariance_covers_the_repo_and_generated_shapes():
    kinds = [k for k, _t in _INVARIANCE]
    assert kinds.count("html") > 200 and kinds.count("js") > 200


@pytest.mark.parametrize("kind,template", _INVARIANCE)
def test_hostile_values_keep_every_template_structure(kind, template):
    """A hole holds a value, never markup: any value renders the same elements and attributes."""
    neutral = shape(kind, render(kind, template, NEUTRAL))
    for value in HOSTILE:
        assert shape(kind, render(kind, template, value)) == neutral, (template, value)

