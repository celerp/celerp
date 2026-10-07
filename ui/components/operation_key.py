# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The key an action form carries from the page that rendered it.

Every rendered form gets its own key, and the route passes it on unchanged, so the
same form submitted twice is recorded once. The next page render brings a new key,
except the page answering an action that failed: whether it happened is not known (its
answer may have been lost), so that page keeps the action's key, and sending it again
is the same action. A key the server says already named a different action is spent: the
page answering that refusal brings a new one.
"""
from __future__ import annotations

import json
import uuid

from fasthtml.common import Input

FIELD = "idempotency_key"


def operation_key_input():
    """Hidden field with a new key, for an action form."""
    return Input(type="hidden", name=FIELD, value=str(uuid.uuid4()))


def operation_key_vals(kept: str = "") -> str:
    """hx-vals carrying a new key (or the ``kept`` one), for an action button with no form of its own."""
    return json.dumps({FIELD: kept or str(uuid.uuid4())})


def operation_key_attrs(kept: str = "") -> dict:
    """A new key (or the ``kept`` one) on a table a bulk toolbar acts on: the toolbar sends it
    with the ticked rows, and the table the action re-renders brings the next one."""
    return {"data-operation-key": kept or str(uuid.uuid4())}


def kept_operation_key(form, error) -> str:
    """The key a failed action's form carried, for the page answering it to keep; none when
    the refusal says that key already named a different action, as sending it again could
    only be refused again."""
    if str((getattr(error, "data", None) or {}).get("message_key") or "").endswith(".key_reused"):
        return ""
    return str(form.get(FIELD, "")).strip()


def submitted_operation_key(form) -> dict:
    """The key a submitted form carried, as keyword arguments to pass on, or none."""
    key = str(form.get(FIELD, "")).strip()
    return {FIELD: key} if key else {}


def required_operation_key(form, action: str = "") -> str:
    """The key a submitted form carried, for a call that cannot go without one.

    One rendered control can offer several actions (a run's action list, a bulk toolbar),
    so the action chosen is part of the key: the same choice sent again is the same
    action, a different choice is a new one. A form carrying no key was rendered before
    the page sent one, and is refused as out of date rather than sent unprotected."""
    from ui.api_client import APIError
    from ui.i18n import t

    key = str(form.get(FIELD, "")).strip()
    if not key:
        raise APIError(400, t("error.page_out_of_date"))
    return f"{key}:{action}" if action else key
