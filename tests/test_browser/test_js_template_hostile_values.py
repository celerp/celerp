# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every js() template, from the repository and generated, run in a real browser with hostile
values: no value runs its own code, and a template that parses with a neutral value still parses
with any other (a value that broke out would leave its trailing "@@" or quote as a syntax error)."""
from __future__ import annotations

import pytest

from encode_templates import HOSTILE, NEUTRAL, generated_templates, render, repo_templates

pytestmark = pytest.mark.browser

_TEMPLATES = sorted({t for k, t in repo_templates() if k == "js"} | set(generated_templates("js")))

_RUN = """(cases) => {
    const out = [];
    for (const [neutral, hostile] of cases) {
        const parses = (src) => { try { new Function(src); return true; } catch (e) { return false; } };
        const before = parses(neutral);
        for (const src of hostile) {
            if (before && !parses(src)) out.push(['syntax', src]);
            const el = document.createElement('script');
            el.textContent = src;
            document.body.appendChild(el);
            el.remove();
            if (window.__pwn !== undefined) { out.push(['ran', src]); delete window.__pwn; }
        }
    }
    return out;
}"""


def test_hostile_values_never_run_in_any_js_template(browser):
    assert len(_TEMPLATES) > 200
    cases = [(render("js", t, NEUTRAL), [render("js", t, v) for v in HOSTILE]) for t in _TEMPLATES]
    page = browser.new_page()
    try:
        page.set_content("<!doctype html><body></body>")
        page.on("dialog", lambda d: d.dismiss())
        bad = page.evaluate(_RUN, cases)
    finally:
        page.close()
    assert bad == [], bad[:10]
