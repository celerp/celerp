# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Permanent guardrail against new hardcoded user-facing strings in ui/.

The i18n sweep routed the app's rendered text through ``t()``. This test keeps it
that way: it scans ui/routes and ui/components for bare English in element
content and in the user-facing attributes (placeholder, title, aria_label,
hx_confirm), and fails if any string is neither wrapped in ``t()`` nor listed,
with a reason, in ``scripts/i18n_allowlist.json``. The allowlist may only shrink.

It also unit-tests the scanner itself so the guardrail cannot silently rot into a
no-op that passes because it stopped detecting anything.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "extract_i18n", _ROOT / "scripts" / "extract_i18n.py"
)
ei = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ei)


def _hit(call_src: str, attr: str | None = None):
    """Parse a single call expression and run the string detector on the first
    positional argument (attr=None) or the named keyword's value."""
    node = ast.parse(call_src, mode="eval").body
    if attr is None:
        return ei._string_hit(node.args[0])
    for kw in node.keywords:
        if kw.arg == attr:
            return ei._string_hit(kw.value)
    return None


# --------------------------------------------------------------------------
# Scanner detection logic
# --------------------------------------------------------------------------

def test_bare_element_text_is_flagged():
    assert _hit('Button("Save changes")') == ("Save changes", False)


def test_t_wrapped_text_is_ignored():
    assert _hit('Button(t("btn.save"))') is None


def test_page_title_wrapped_is_ignored():
    assert _hit('Title(page_title("nav.inventory"))') is None


def test_non_translatable_constants_ignored():
    assert _hit('Span("0.00")') is None   # numeric format hint
    assert _hit('Span("--")') is None     # empty-value marker
    assert _hit('Span("#")') is None      # symbol


def test_fstring_with_english_is_flagged():
    hit = _hit('Span(f"{n} items left")')
    assert hit is not None and hit[1] is True and "items left" in hit[0]


def test_placeholder_attr_is_flagged():
    assert _hit('Input(placeholder="Search orders")', attr="placeholder") == (
        "Search orders",
        False,
    )


def test_placeholder_attr_t_wrapped_ignored():
    assert _hit('Input(placeholder=t("x.search"))', attr="placeholder") is None


def test_hx_confirm_attr_is_flagged():
    assert _hit('Button("x", hx_confirm="Delete this item?")', attr="hx_confirm") == (
        "Delete this item?",
        False,
    )


def test_scan_source_finds_positional_and_attr_together():
    src = 'x = Div(H2("Reports"), Input(placeholder="Type a name"))'
    texts = {r["text"] for r in ei._scan_source(src, "ui/routes/fake.py")}
    assert "Reports" in texts
    assert "Type a name" in texts


def test_scan_source_ignores_fully_translated():
    src = 'x = Div(H2(t("a")), Input(placeholder=t("b")), Button(t("c"), hx_confirm=t("d")))'
    assert ei._scan_source(src, "ui/routes/fake.py") == []


# --------------------------------------------------------------------------
# The permanent guardrail
# --------------------------------------------------------------------------

def test_no_new_hardcoded_ui_strings():
    leaks = ei.scan(ei.UI_SCAN_PATHS, apply_allowlist=True)
    detail = "\n".join(
        f"  {r['file']}:{r['line']} {r['element']} "
        f"{(r.get('attr') or 'text')}={r['text']!r}"
        for r in leaks
    )
    assert not leaks, (
        "New hardcoded UI string(s) found. Route each through t(), or add a "
        "reasoned exception to scripts/i18n_allowlist.json:\n" + detail
    )


def test_allowlist_has_no_dead_entries():
    """Every allowlist entry must still match a real string in ui/, so a string
    that gets translated cannot leave a stale exception behind. The list shrinks,
    never ossifies."""
    live = {(r["file"], r["text"]) for r in ei.scan(ei.UI_SCAN_PATHS)}
    stale = sorted(ei._load_allowlist() - live)
    assert not stale, f"Stale allowlist entries (string no longer present): {stale}"
