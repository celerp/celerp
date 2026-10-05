"""Choosing the inventory account of older stock: every refusal reaches the reader in their
language, and a refusal for stock no inventory account holds names the entry that places it.

Each refusal from PUT /accounting/posting-accounts/older-stock/{item} is a structured refusal
(message_key + params), so ui.i18n.refusal_text renders it like every other posting refusal.
When no inventory account holds the stock's value, the refusal names the journal entry that
recognises it (inventory against retained earnings, as the opening-stock entry does), and
posting that entry lets the same choice through."""
from __future__ import annotations

import uuid

import pytest

from celerp.services.auto_je import _emit_auto_posted_je
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_posting_accounts_panel import _account, _put
from test_posting_roles_lots import _forget_origin, _lot
from test_posting_roles_older_stock import _accounts, _choose, _older_release, _restored
from test_posting_roles_rollout import _startup
from ui import i18n

pytestmark = pytest.mark.asyncio


def _in(lang: str, detail) -> str:
    i18n.set_lang(lang)
    try:
        return i18n.refusal_text(detail)
    finally:
        i18n.set_lang("en")


async def test_every_older_stock_refusal_reads_in_the_readers_language(session, client, auth):
    await _account(client, auth, "1135", "asset", "1100")
    assert (await _put(client, auth, "inventory_purchased", "1135")).status_code == 200
    lot = await _lot(client, auth, 30.0, sku="OLD-1")
    await _forget_origin(session, auth, lot)
    cases = {
        "never held inventory": "1120",
        "not enough held": "1135",
        "no account chosen": " ",
        "an account that does not exist": "9999-NONE",
    }
    bad = []
    for label, code in cases.items():
        r = await _choose(client, auth, lot, code)
        assert r.status_code in (409, 422), (label, r.status_code, r.text)
        detail = r.json()["detail"]
        if not (isinstance(detail, dict) and detail.get("message_key")):
            bad.append((label, detail))
        elif _in("en", detail) != detail["message"]:
            bad.append((label, "English text differs from the server's", _in("en", detail), detail["message"]))
        elif _in("de", detail) == detail["message"]:
            bad.append((label, "German text equals English", detail))
    assert not bad, bad


async def test_stock_no_account_holds_is_refused_naming_the_entry_that_places_it(session, client, auth):
    await _older_release(session, auth)
    await _restored(session, client, auth)
    lot = await _lot(client, auth, 30.0)
    await _startup(session)
    assert await _accounts(session, auth, lot) == [None]

    r = await _choose(client, auth, lot, "1130-OB")
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    entry = "post a journal entry Dr 1130-OB 30.00 / Cr 3200 30.00, then choose 1130-OB again."
    assert entry in detail["message"], detail
    assert entry in _in("en", detail)
    german = _in("de", detail)
    assert "Soll 1130-OB 30.00 / Haben 3200 30.00" in german and "books need reconciling" not in german, german

    cid, je = auth["company_id"], f"je:{uuid.uuid4()}"
    await _emit_auto_posted_je(
        session, company_id=cid, user_id=auth["user_id"], je_id=je, idem_create=f"{je}:c",
        idem_posted=f"{je}:p", memo="Recognise older stock", metadata_={"trigger": "manual"},
        entries=[{"account": "1130-OB", "debit": 30.0, "credit": 0.0},
                 {"account": "3200", "debit": 0.0, "credit": 30.0}])
    await session.commit()
    r = await _choose(client, auth, lot, "1130-OB")
    assert r.status_code == 200, r.text
    assert await _accounts(session, auth, lot) == ["1130-OB"]
