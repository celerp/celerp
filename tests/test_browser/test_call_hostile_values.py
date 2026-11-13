# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Hostile values through every way a value reaches a page, read back by a real browser.

``call()`` output is run in an inline script and in an event attribute: each must be exactly
one call of its function with exactly the values given, and no value runs code of its own.
Every attribute name the sink check lets take a computed value is built by FastHTML with the
same values and read back by ``html.parser`` and by Chromium: one element, one attribute,
the value unchanged.
"""
from __future__ import annotations

import importlib.util
import random
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fasthtml.common import Body, Button, Div, Head, Html, Script, to_xml

from encode_values import hostile_string, hostile_values
from ui.components.encode import call

pytestmark = pytest.mark.browser

_DRAWS = 300
_RECORDER = "window.__calls = []; window.__rec = function () { window.__calls.push([].slice.call(arguments)); };"


def _dynamic_attrs() -> list[str]:
    spec = importlib.util.spec_from_file_location(
        "sink_check", Path(__file__).resolve().parents[2] / "scripts" / "sink_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return sorted(mod.DYNAMIC_ATTRS) + [p + "x" for p in mod.DYNAMIC_ATTR_PREFIXES]


def _draws(seed: int) -> list[list]:
    rng = random.Random(seed)
    return [[rng.choice(hostile_values(rng)) for _ in range(rng.randint(0, 4))] for _ in range(_DRAWS)]


def _open(browser, markup: str):
    page = browser.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.dismiss())
    page.set_content(markup)
    return page, errors


def test_call_is_one_call_of_its_values_in_a_script_and_in_a_handler(browser):
    draws = _draws(4251)
    expected = [[i, *values] for i, values in enumerate(draws)]
    markup = to_xml(Html(Head(Script(_RECORDER)), Body(
        *(Script(call("__rec", i, *values)) for i, values in enumerate(draws)),
        *(Button("x", onclick=call("__rec", i, *values)) for i, values in enumerate(draws)))))
    page, errors = _open(browser, markup)
    try:
        from_scripts = page.evaluate("window.__calls.splice(0)")
        page.evaluate("document.querySelectorAll('button').forEach(b => b.click())")
        from_handlers = page.evaluate("window.__calls.splice(0)")
        pwn = page.evaluate("window.__pwn")
        buttons = page.evaluate("document.querySelectorAll('body > button').length")
    finally:
        page.close()
    assert errors == []
    assert pwn is None
    assert buttons == _DRAWS
    assert from_scripts == expected
    assert from_handlers == expected


class _Attrs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list]] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    handle_startendtag = handle_starttag


def _kwarg(name: str) -> str:
    return "_" + name.replace("-", "_")  # FastHTML drops the leading underscore and writes "-" for "_"


def _browser_text(v: str) -> str:
    """What an HTML parser hands the DOM for attribute text: newlines normalised, NUL replaced."""
    return v.replace("\r\n", "\n").replace("\r", "\n").replace("\0", "�")


def test_every_dynamic_attribute_keeps_its_value_and_stays_one_attribute(browser):
    rng = random.Random(4252)
    cases = [(name, hostile_string(rng) or "x") for name in _dynamic_attrs() for _ in range(40)]
    markup = to_xml(Html(Body(*(Div(**{_kwarg(n): v}) for n, v in cases))))

    parser = _Attrs()
    parser.feed(markup)
    parser.close()
    divs = [attrs for tag, attrs in parser.tags if tag == "div"]
    assert divs == [[(n, v)] for n, v in cases]

    page, errors = _open(browser, markup)
    try:
        got = page.evaluate("""() => [...document.body.children].map(el =>
            [el.tagName, [...el.attributes].map(a => [a.name, a.value])])""")
        pwn = page.evaluate("window.__pwn")
    finally:
        page.close()
    assert errors == [] and pwn is None
    assert got == [["DIV", [[n, _browser_text(v)]]] for n, v in cases]
