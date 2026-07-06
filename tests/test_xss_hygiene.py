# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""UI XSS-hygiene: attribute-context injection (2026-06-29 plan).

Data interpolated into hx_vals / onclick / hx_confirm / Script bodies must be
escaped or JSON-encoded, never hand-built with an f-string. These tests prove the
helpers escape adversarial input and a repo-grep guard prevents regressions.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from ui.components.attrs import esc_attr, hx_vals

_UI_DIR = Path(__file__).resolve().parents[1] / "ui"


def test_hx_vals_escapes_json_breakout():
    # A query param that tries to close the JSON object and inject a new key.
    evil = 'foo"},"evil":"bar'
    out = hx_vals({"search": evil, "page": "1"})
    # Valid JSON that round-trips to the original value (no breakout / extra keys).
    parsed = json.loads(out)
    assert parsed == {"search": evil, "page": "1"}
    assert "evil" not in {k for k in parsed if k != "search"}


def test_esc_attr_escapes_quotes_and_brackets():
    out = esc_attr('"><script>alert(1)</script>')
    assert "<script>" not in out
    assert '"' not in out  # quote is escaped (&quot;)


def test_no_fstring_hx_vals_in_ui_tree():
    """Guard: hx_vals must never be built by f-string interpolation. Use hx_vals()."""
    offenders = []
    pattern = re.compile(r"""hx_vals\s*=\s*f['"]""")
    for path in _UI_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(_UI_DIR)}:{i}")
    assert not offenders, f"f-string hx_vals found (use hx_vals()): {offenders}"
