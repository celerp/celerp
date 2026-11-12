# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The count of outside records waiting for a fund choice: held items a connected company
sent that wait for funds, plus records that came from outside with no posted entry of
their own and a line or header no choice covers. Shown on the dashboard with Choose funds
(the filtered document list) and Sync accounts for held items.

Initial conditions: a fresh company with the seeded chart; outside drafts arrive through
the document import (origin ``file_import``); held items are written as the peer sync
writes them (peer_attention.record).
"""
from __future__ import annotations

import uuid

import pytest

from test_dashboard_getting_started import _Dash, _dashboard, ui  # noqa: F401 (fixture)
from test_income_accounts import _draft, _item, _line
from test_outside_lines import _choose, _fund_company, _imported

pytestmark = pytest.mark.asyncio

_COUNT = "3 outside documents waiting for a fund choice"


async def _hold(session, auth, kind: str | None) -> None:
    from celerp.services import peer_attention

    await peer_attention.record(
        session, company_id=auth["company_id"], source_company_id=uuid.uuid4(), entity_type="journal_entry",
        source_entity_id=uuid.uuid4().hex, label="Correction", reason="Waiting for a fund on each line",
        fix={"kind": kind} if kind else None)
    await session.commit()


async def _waiting(client, headers) -> dict:
    r = await client.get("/accounting/funds/waiting", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _operator(session, auth) -> dict:
    from celerp.models.accounting import UserCompany
    from celerp.models.company import User
    from test_helpers import make_authed_token

    uid = uuid.uuid4()
    session.add(User(id=uid, email=f"op-{uid.hex[:8]}@test.co", name="Operator", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=auth["company_id"], role="operator", is_active=True))
    await session.commit()
    return {"Authorization": f"Bearer {await make_authed_token(session, str(uid), str(auth['company_id']), 'operator')}"}


async def test_waiting_for_fund_choice_count_shown(client, session, auth):
    """Two held items and one uncovered draft count 3; a held item fixed elsewhere, a local
    draft and an outside draft whose document choice covers it do not. The document list
    filtered by fund_choice=needed holds exactly the uncovered draft. Funds off counts 0 and
    a user without the fund permission is refused."""
    from celerp.services.peer_documents import DOC_FUNDS

    item = await _item(client, auth, "Service")
    await _hold(session, auth, "funds")
    await _hold(session, auth, DOC_FUNDS)
    await _hold(session, auth, None)
    waiting = await _imported(client, auth, [_line(item, "Outside", 30.0)])
    covered = await _imported(client, auth, [_line(item, "Covered", 20.0)])
    await _draft(client, auth, "invoice", [_line(item, "Local", 10.0)])

    assert await _waiting(client, auth["headers"]) == {"count": 0, "held": 0, "documents": []}

    fund = await _fund_company(client, auth)
    r = await _choose(client, auth, covered, document=fund)
    assert r.status_code == 200, r.text
    assert await _waiting(client, auth["headers"]) == {"count": 3, "held": 2, "documents": [waiting]}

    r = await client.get("/docs", headers=auth["headers"], params={"fund_choice": "needed"})
    assert r.status_code == 200, r.text
    assert [row["id"] for row in r.json()["items"]] == [waiting]

    r = await _choose(client, auth, waiting, document="unrestricted")
    assert r.status_code == 200, r.text
    assert await _waiting(client, auth["headers"]) == {"count": 2, "held": 2, "documents": []}

    r = await client.get("/accounting/funds/waiting", headers=await _operator(session, auth))
    assert r.status_code == 403, r.text


def _banner_api(waiting: dict):
    from unittest.mock import AsyncMock, patch

    return patch("ui.api_client.get_fund_waiting", new=AsyncMock(return_value=waiting))


async def test_waiting_for_fund_choice_banner_on_dashboard(ui):  # noqa: F811
    """The dashboard shows the count with Choose funds and, for held items, Sync accounts;
    absent at 0, with funds off, and for a role without the fund permission."""
    funds_on = {"funds_enabled": True}
    shown = {"count": 3, "held": 2, "documents": ["doc:a"]}
    with _banner_api(shown):
        html = await _dashboard(ui, settings=funds_on)
        assert _COUNT in html
        assert 'href="/docs?fund_choice=needed"' in html
        assert 'href="/accounting/sync-accounts"' in html
        assert _COUNT not in await _dashboard(ui, settings={})
        assert _COUNT not in await _dashboard(ui, role="operator", settings=funds_on)
    with _banner_api({"count": 1, "held": 0, "documents": ["doc:a"]}):
        html = await _dashboard(ui, settings=funds_on)
        assert "1 outside document waiting for a fund choice" in html
        assert "/accounting/sync-accounts" not in html
    with _banner_api({"count": 0, "held": 0, "documents": []}):
        assert "waiting for a fund choice" not in await _dashboard(ui, settings=funds_on)
