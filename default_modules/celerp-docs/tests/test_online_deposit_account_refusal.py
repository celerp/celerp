# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Online payments refused for an account that cannot take them say why, in the
reader's own language.

require_online_deposit_account used to catch its own inner refusal and compose a
brand new English sentence around its message, discarding the inner message_key -
so a th reader saw the same English text an en reader did. It now raises a single
properly keyed refusal, which ui/i18n.refusal_text translates like any other."""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from celerp.accounting_roles import AccountRole
from celerp.services.account_roles import resolve
from celerp_docs.routes_payments import require_online_deposit_account
from ui.i18n import refusal_text, set_lang, t


@pytest.mark.asyncio
async def test_deposit_account_refusal_is_translated_for_the_readers_language(session, auth):
    company_id = auth["company_id"]
    default = await resolve(session, company_id, AccountRole.DEFAULT_DEPOSIT)
    code = f"no-such-account-{uuid.uuid4().hex[:8]}"

    with pytest.raises(HTTPException) as exc:
        await require_online_deposit_account(session, company_id, code)

    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["message_key"] == "documents.err_deposit_account_refused"
    assert detail["params"] == {"code": code, "default": default}

    set_lang("en")
    english = refusal_text(detail)
    assert english == t("documents.err_deposit_account_refused", "en", code=code, default=default)

    set_lang("th")
    try:
        thai = refusal_text(detail)
    finally:
        set_lang("en")
    assert thai == t("documents.err_deposit_account_refused", "th", code=code, default=default)
    assert thai != english
    assert code in thai and default in thai
