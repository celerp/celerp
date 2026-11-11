# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""UI XSS-hygiene: attribute-context injection (2026-06-29 plan).

Data interpolated into hx_vals / onclick / hx_confirm / Script bodies must be
escaped or JSON-encoded, never hand-built with an f-string. These tests prove the
helpers escape adversarial input; scripts/sink_check.py (tests/test_sink_check.py) refuses the
hand-built shapes.
"""
from __future__ import annotations

import json

from ui.components.attrs import hx_vals
from ui.components.encode import esc_attr


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

