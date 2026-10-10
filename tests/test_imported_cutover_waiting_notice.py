# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The start-up correction of earlier imports names the documents it could not correct.

Initial conditions per test: a fresh company with the seeded chart, a lot of 10 units
booked as opening stock, and a purchase order an earlier release imported and booked
again (_legacy). The correction runs at start-up (_repair). A document it cannot correct
yet, because no open date exists to post on (a lock covering today) or because the
correction failed, is named to the company in one high-priority notice per reason, with
what to do next; it is told once per list, not at every start.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.models.notification import Notification
from celerp.services import auto_je
from test_cost_restatement import _item, _state
from test_imported_document_cutover import _legacy, _repair
from test_receipt_accounting import _OPENING

pytestmark = pytest.mark.asyncio

_WAITING = "notice.imported_doc_cutover.waiting_title"


async def _notices(session, auth, title_key: str) -> list:
    """The company's notices with this title key, read into plain values."""
    session.expire_all()
    rows = (await session.execute(select(Notification).where(
        Notification.company_id == auth["company_id"], Notification.category == "system",
        Notification.i18n["title"].as_string() == title_key))).scalars().all()
    return [SimpleNamespace(priority=n.priority, body=n.body, i18n=dict(n.i18n or {})) for n in rows]


async def _lock(client, auth, day) -> None:
    r = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": day})
    assert r.status_code == 200, r.text


async def test_a_document_with_no_open_date_is_named_in_the_notice(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    await _lock(client, auth, "2999-12-31")
    assert (await _repair(session))["deferred"] == 1
    await _repair(session)
    notices = await _notices(session, auth, _WAITING)
    assert len(notices) == 1 and notices[0].priority == "high", notices
    assert notices[0].i18n["body"] == "notice.imported_doc_cutover.locked"
    number = (await _state(session, auth, po))["doc_number"]
    assert number in notices[0].body and notices[0].i18n["params"]["numbers"] == number
    assert "Settings > Accounting" in notices[0].body


async def test_a_document_the_correction_failed_on_is_named_in_the_notice(client, session, auth, monkeypatch):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)

    async def refuse(*_a, **_k):
        raise HTTPException(status_code=422, detail="not correctable")

    monkeypatch.setattr(auto_je, "correct_earlier_import", refuse)
    assert (await _repair(session))["errored"] == 1
    notices = await _notices(session, auth, _WAITING)
    assert [n.i18n["body"] for n in notices] == ["notice.imported_doc_cutover.failed"], notices
    assert (await _state(session, auth, po))["doc_number"] in notices[0].body


# Neighbouring rules: a corrected document is still named in the corrected notice, and a run
# that corrects everything tells no one about waiting documents.


async def test_a_clean_run_names_only_what_it_corrected(client, session, auth):
    lot = await _item(client, auth, _OPENING, qty=10)
    po = await _legacy(client, session, auth, lot)
    assert (await _repair(session))["corrected"] == 1
    assert await _notices(session, auth, _WAITING) == []
    corrected = await _notices(session, auth, "notice.imported_doc_cutover.title")
    assert len(corrected) == 1 and (await _state(session, auth, po))["doc_number"] in corrected[0].body
