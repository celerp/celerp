# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Safe attribute helpers for the FastHTML UI.

FastHTML auto-escapes *text* passed to elements, but data interpolated into
ATTRIBUTES (hx_vals, onclick, hx_confirm) or Script() bodies via f-strings is
NOT escaped and can break out. Use these helpers whenever an attribute value is
built from non-constant data (query params, CSV headers, filenames, user input).
"""
from __future__ import annotations

import html
import json


def hx_vals(values: dict) -> str:
    """Serialize a dict for an ``hx_vals`` attribute as valid, escaped JSON.

    Always use this instead of hand-building the JSON with an f-string: a value
    like ``foo"},"evil":"bar`` is escaped inside the string rather than closing
    the JSON object and injecting new keys.
    """
    return json.dumps(values, separators=(",", ":"))


def esc_attr(value: object) -> str:
    """HTML-escape a value for safe interpolation into an attribute (quotes too)."""
    return html.escape(str(value), quote=True)
