# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The key an action form carries from the page that rendered it.

Every rendered form gets its own key, and the route passes it on unchanged, so the
same form submitted twice is recorded once. The next page render brings a new key.
"""
from __future__ import annotations

import json
import uuid

from fasthtml.common import Input

FIELD = "idempotency_key"


def operation_key_input():
    """Hidden field with a new key, for an action form."""
    return Input(type="hidden", name=FIELD, value=str(uuid.uuid4()))


def operation_key_vals() -> str:
    """hx-vals carrying a new key, for an action button with no form of its own."""
    return json.dumps({FIELD: str(uuid.uuid4())})


def submitted_operation_key(form) -> dict:
    """The key a submitted form carried, as keyword arguments to pass on, or none."""
    key = str(form.get(FIELD, "")).strip()
    return {FIELD: key} if key else {}
