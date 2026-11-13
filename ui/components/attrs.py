# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The ``hx_vals`` attribute helper for the FastHTML UI.

Values placed in script, style or URL positions, or admitted as raw markup, go
through ``ui.components.encode``.
"""
from __future__ import annotations

import json


def hx_vals(values: dict) -> str:
    """Serialize a dict for an ``hx_vals`` attribute as valid, escaped JSON.

    Always use this instead of hand-building the JSON with an f-string: a value
    like ``foo"},"evil":"bar`` is escaped inside the string rather than closing
    the JSON object and injecting new keys.
    """
    return json.dumps(values, separators=(",", ":"))
