"""Finishing a migration with posting accounts unset, or set in a way the books cannot take,
is refused with keyed refusals, so the verify page reads in the user's language and names
each posting account by the label the user's language gives it. Nothing is saved."""
from __future__ import annotations

import pytest

from test_posting_roles_finalize import _CHOICES, _codes, _company, _finalize, _ready_run
from test_posting_roles_migration_sinks import _import_chart, _staged_context
from ui import i18n

pytestmark = pytest.mark.asyncio


def _in(lang: str, detail) -> str:
    i18n.set_lang(lang)
    try:
        return i18n.refusal_text(detail)
    finally:
        i18n.set_lang("en")


async def _refused(session, monkeypatch, choices=None):
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.migrations import MigrationError

    context = await _staged_context(session)
    await _import_chart(context)
    await _ready_run(context, monkeypatch)
    before = await _codes(context)
    with pytest.raises(MigrationError) as exc:
        await _finalize(context, monkeypatch, choices)
    assert exc.value.status_code == 409
    assert ROLES_KEY not in ((await _company(context)).settings or {})
    assert await _codes(context) == before
    detail = exc.value.detail
    assert isinstance(detail, dict) and detail.get("message_key"), detail
    return detail


async def test_finishing_with_posting_accounts_unset_is_refused_naming_them_in_german(session, monkeypatch):
    detail = await _refused(session, monkeypatch)
    assert "Choose the posting account for: " in detail["message"] and "Sales revenue" in detail["message"]
    assert _in("en", detail) == detail["message"]
    german = _in("de", detail)
    assert "Sales revenue" not in german and i18n.t("posting.role.sales_revenue", lang="de") in german, german


async def test_finishing_with_an_account_of_the_wrong_type_is_refused_in_german(session, monkeypatch):
    detail = await _refused(session, monkeypatch, {**_CHOICES, "roles": {**_CHOICES["roles"], "sales_revenue": "210"}})
    assert "Sales revenue is set to account 210, of type liability; it must be of type revenue." in detail["message"]
    german = _in("de", detail)
    assert "must be of type" not in german and "210" in german, german


async def test_finishing_with_an_added_account_missing_its_name_is_refused_in_german(session, monkeypatch):
    detail = await _refused(session, monkeypatch, {"roles": {}, "add_accounts": [
        {"code": "6950", "name": " ", "account_type": "expense", "role": "general_expense"}]})
    assert detail["message"] == "Each added account needs a name." == _in("en", detail)
    assert _in("de", detail) != detail["message"]


@pytest.mark.parametrize(("account", "message"), [
    ({"code": "K" * 33}, "Account code must be 32 characters or fewer."),
    ({"account_type": "money"}, "Account type must be one of: asset, liability, equity, revenue, cogs, expense, other."),
    ({"name": "General\x00expenses"}, "Account name cannot contain a NUL character."),
])
async def test_an_added_account_the_chart_refuses_is_refused_in_german(session, monkeypatch, account, message):
    first, *rest = _CHOICES["add_accounts"]
    detail = await _refused(session, monkeypatch, {**_CHOICES, "add_accounts": [{**first, **account}, *rest]})
    assert detail["message"] == message == _in("en", detail)
    assert _in("de", detail) != message, _in("de", detail)
