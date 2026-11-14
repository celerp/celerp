# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Whether a journal line is a gift is the company's answer, given once at the posting boundary
from its gift-account rules (a line naming a contact on a gift income account), never the
caller's: every way a new entry reaches the boundary (a journal batch import, a migration, a
manual entry), with ``gift`` supplied True on a line on an account that is not a gift account and
False on a line on a gift account, posts the rules' answer, whatever else the file claims about
the line (that it copies a posted entry, that it posts for a record). A replay keeps what was
stored: a document's entry restored by an unvoid and the ledger replayed into fresh projections
keep the classification they posted with, after the rules changed.

Initial conditions per test: a fresh company with the seeded chart, funds off, account 4260
(revenue) credited by a category that is a gift category when the case needs a gift account."""
from __future__ import annotations

import uuid

import pytest

from income_books import PERIOD, _account, _books, _category, _donor, _invoice, _line, _set_category_account

pytestmark = pytest.mark.asyncio

ACCOUNT = "4260"


async def _history_total(client, auth, donor: str) -> float:
    r = await client.get(f"/donors/{donor}/history", headers=auth["headers"], params=PERIOD)
    assert r.status_code == 200, r.text
    return float(r.json()["total"])


async def _gift_setting(client, auth, category: str, gift: bool) -> None:
    r = await client.put(f"/companies/me/categories/{category}/gift", headers=auth["headers"], json={"gift": gift})
    assert r.status_code == 200, r.text


def _lines(donor: str, gift: bool, **claims) -> list[dict]:
    return [{"account": "1111", "debit": 100.0, "credit": 0.0},
            {"account": ACCOUNT, "debit": 0.0, "credit": 100.0, "contact": donor, "gift": gift, **claims}]


def _record(entries: list[dict], **header) -> dict:
    return {"entity_id": f"je:{uuid.uuid4()}", "event_type": "acc.journal_entry.created", "source": "csv",
            "idempotency_key": uuid.uuid4().hex,
            "data": {"status": "posted", "ts": "2026-03-10", "memo": "Bank transfer", "entries": entries, **header}}


async def _posted_entry(client, auth) -> str:
    """A posted manual entry naming no party, for a file to claim its line copies."""
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-09", "memo": "Float", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 5.0, "credit": 0.0}, {"account": ACCOUNT, "debit": 0.0, "credit": 5.0}]})
    assert r.status_code == 200, r.text
    return r.json()["je_id"]


async def _batch(client, auth, record: dict) -> None:
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [record]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text


async def _post_import(client, auth, session, donor, gift):
    await _batch(client, auth, _record(_lines(donor, gift)))


async def _post_import_claiming_a_copy(client, auth, session, donor, gift):
    await _batch(client, auth, _record(_lines(donor, gift, posted_from=await _posted_entry(client, auth))))


async def _post_import_claiming_a_record(client, auth, session, donor, gift):
    await _batch(client, auth, _record(_lines(donor, gift), record_id=await _posted_entry(client, auth)))


async def _post_migration(client, auth, session, donor, gift):
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    from celerp_accounting.import_service import AccImportRecord, import_journal_records

    company = uuid.UUID(str(auth["company_id"]))
    user = (await session.execute(select(UserCompany.user_id).where(UserCompany.company_id == company))).scalars().first()
    outcome = await import_journal_records(
        session, company, user, [AccImportRecord(**_record(_lines(donor, gift)))], origin="migration")
    await session.commit()
    assert [r.status for r in outcome.records] == ["created"], outcome


async def _post_manual(client, auth, session, donor, gift):
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-03-10", "memo": "Bank transfer", "idempotency_token": uuid.uuid4().hex,
        "entries": _lines(donor, gift)})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("gift_account", [False, True], ids=["true-on-other-account", "false-on-gift-account"])
@pytest.mark.parametrize("post", [_post_import, _post_import_claiming_a_copy, _post_import_claiming_a_record,
                                  _post_migration, _post_manual],
                         ids=["import", "import-claiming-copy", "import-claiming-record", "migration", "manual"])
async def test_a_new_entry_is_a_gift_by_the_company_rules_never_by_the_caller(client, auth, session, post, gift_account):
    await _account(client, auth, ACCOUNT)
    category = await _category(client, auth, "Donations by transfer")
    assert (await _set_category_account(client, auth, category, ACCOUNT)).status_code == 200
    if gift_account:
        await _gift_setting(client, auth, category, True)
    donor = await _donor(client, auth, "Transfer Donor")
    await post(client, auth, session, donor, gift=not gift_account)
    assert await _history_total(client, auth, donor) == (100.0 if gift_account else 0.0)


async def test_an_unvoided_document_and_a_replayed_ledger_keep_the_gift_they_posted_with(client, auth, session):
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    items = await _books(client, auth)
    donor = await _donor(client, auth, "Ada Donor")
    doc = await _invoice(client, auth, donor, _line(items["gift"], "Gift", 100))
    assert await _history_total(client, auth, donor) == 100.0
    donation = (await client.get(f"/items/{items['gift']}", headers=auth["headers"])).json()["category"]
    await _gift_setting(client, auth, donation, False)
    r = await client.post(f"/docs/{doc}/void", headers=auth["headers"], json={"reason": "Wrong date"})
    assert r.status_code == 200, r.text
    assert await _history_total(client, auth, donor) == 0.0
    r = await client.post(f"/docs/{doc}/unvoid", headers=auth["headers"], json={})
    assert r.status_code == 200, r.text
    assert await _history_total(client, auth, donor) == 100.0
    company = uuid.UUID(str(auth["company_id"]))
    await lock_company(session, company)
    await ProjectionEngine.rebuild(session, company)
    await session.commit()
    assert await _history_total(client, auth, donor) == 100.0
