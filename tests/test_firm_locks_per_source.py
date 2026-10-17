# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Each accountant's lock date is kept for the key it came through, and the latest one
still in force is the one that binds: a second firm with an earlier date never opens a
period the first still holds closed, in either sync order and after the client moves its
own lock back. Each firm's date moves the client's own lock forward once. A lock ends
when its key is revoked or expires, and a renewed key brings it back without moving the
client's lock again."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from company_backup_support import confirm, download, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link, _link_verified  # noqa: F401
from test_peer_sync_end_to_end import _route_to_app
from test_peer_two_way_sync import _lock, _notices, _pair_journals, _post_journal, _received

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]  # two firms each open a copy

MAY = "2026-05-15"


async def _run(company_id: str, credential: str) -> dict:
    """One run of the connector both ways; an entity that fails is kept as its error."""
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.celerp import CelerpConnector
    from celerp.connectors.sync_runner import sync_plan
    from test_peer_two_way_sync import FIRM

    connector = CelerpConnector()
    ctx = ConnectorContext(company_id=company_id, store_handle=FIRM, access_token=credential)
    results = {}
    for entity in sync_plan(connector, SyncDirection.BOTH):
        try:
            results[entity] = await getattr(connector, f"sync_{entity}")(ctx)
        except Exception as exc:  # the run reports it; the next entity still runs
            results[entity] = exc
    return results


async def _second_firm(real_client, real_engine, tok: str, client_company: str) -> tuple[str, str]:
    """A second accountant opening its own copy of the client's company, before either
    firm has synced, and the key it issues the client: (its token, the key)."""
    from company_backup_support import company

    accountant = await owner(real_engine, "second-firm@example.test", "Second Owner")
    own = await company(real_engine, accountant, "Second Practice", "second-marker")
    second_tok = await token(real_engine, accountant, own)
    preview = await real_client.post("/company-backups/read", files={
        "file": ("demo.celerp-company", await download(real_client, tok))},
        data={"mode": "new_company", **_link()}, headers=auth(second_tok))
    assert preview.status_code == 200, preview.text
    opened = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"),
                                    headers=auth(second_tok))
    assert opened.status_code == 201, opened.text
    second_tok = await token(real_engine, accountant, opened.json()["company_id"])
    granted = await real_client.post("/company-peers/grants", json={
        "source_company_id": client_company, "label": "Demo Trading",
        "scopes": ["contacts", "products", "documents", "inventory", "journals"]},
        headers=auth(second_tok))
    assert granted.status_code == 201, granted.text
    return second_tok, granted.json()["credential"]


async def _period_lock(real_client, tok: str) -> dict:
    r = await real_client.get("/accounting/period-lock", headers=auth(tok))
    assert r.status_code == 200, r.text
    return r.json()


async def _may_journal_held_until_june(real_client, real_engine, monkeypatch):
    """The client keeps two firms; the first posts a journal dated in May, the client takes
    it, the firm closes June, the client's own lock follows, and the client moves its own
    lock back to January."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    second = await _second_firm(real_client, real_engine, tok, client_company)
    _route_to_app(monkeypatch, [])
    await _post_journal(real_client, firm_tok, MAY, 40.0, "May accrual")
    await _run(client_company, credential)
    await _lock(real_client, firm_tok, "2026-06-30")
    await _run(client_company, credential)
    assert (await _period_lock(real_client, tok))["lock_date"] == "2026-06-30"
    await _lock(real_client, tok, "2026-01-31")
    [row] = await _received(real_client, tok)
    return tok, firm_tok, client_company, credential, row["id"], second


@pytest.mark.parametrize("second_first", [False, True], ids=["first-then-second", "second-then-first"])
async def test_an_earlier_lock_from_a_second_firm_never_opens_the_first_firms_period(
        real_client, real_engine, monkeypatch, second_first):
    """Firm A holds June closed and firm B March. Whichever syncs last, and after the
    client moves its own lock back, A's June still binds: the A journal from May is shown
    as reversing today, and its reversal posts today, never on its May date."""
    tok, firm_tok, client_company, credential, local, (second_tok, second_key) = (
        await _may_journal_held_until_june(real_client, real_engine, monkeypatch))
    await _lock(real_client, second_tok, "2026-03-31")
    order = [(second_key,), (credential,), (second_key,)] if second_first else [(credential,), (second_key,)]
    for (key,) in order:
        await _run(client_company, key)

    lock = await _period_lock(real_client, tok)
    assert lock["lock_date"] == "2026-03-31", "B's date moves it once; A's, already applied, never again"
    assert lock["firm_lock"] == {"date": "2026-06-30", "firm": "Firm Practice"}, lock
    [row] = [r for r in await _received(real_client, tok) if r["id"] == local]
    assert row["reverse_on"] != MAY and row["reverse_on"] > "2026-06-30", row
    reversed_ = await real_client.post(f"/accounting/journal-entries/{local}/reverse", headers=auth(tok))
    assert reversed_.status_code == 200, reversed_.text
    assert reversed_.json()["ts"] > "2026-06-30", reversed_.json()


async def test_each_firm_moves_the_client_lock_forward_once(real_client, real_engine, monkeypatch):
    """Firm A closes March and firm B June: each moves the client's own lock forward once,
    with one notice each. Once the client moves its lock back, neither firm's unchanged
    date moves it again, whichever syncs next."""
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    _route_to_app(monkeypatch, [])
    second_tok, second_key = await _second_firm(real_client, real_engine, tok, client_company)
    await _lock(real_client, firm_tok, "2026-03-31")
    await _lock(real_client, second_tok, "2026-06-30")
    await _run(client_company, credential)
    assert (await _period_lock(real_client, tok))["lock_date"] == "2026-03-31"
    await _run(client_company, second_key)
    assert (await _period_lock(real_client, tok))["lock_date"] == "2026-06-30"

    await _lock(real_client, tok, "2026-01-31")
    for key in (credential, second_key, credential):
        await _run(client_company, key)
        assert (await _period_lock(real_client, tok))["lock_date"] == "2026-01-31"
    notices = [body for _, body, _ in await _notices(real_engine, client_company)]
    assert len([b for b in notices if "Firm Practice" in b and "2026-03-31" in b]) == 1, notices
    assert len([b for b in notices if "Second Practice" in b and "2026-06-30" in b]) == 1, notices


async def test_a_revoked_key_ends_its_firms_lock_and_leaves_the_others(real_client, real_engine, monkeypatch):
    """Firm A (June) revokes the client's key; at the client's next run with that key A's
    lock no longer binds, while firm B's March still does."""
    tok, firm_tok, client_company, credential, local, (second_tok, second_key) = (
        await _may_journal_held_until_june(real_client, real_engine, monkeypatch))
    await _lock(real_client, second_tok, "2026-03-31")
    await _run(client_company, second_key)
    await _run(client_company, credential)
    assert (await _period_lock(real_client, tok))["firm_lock"]["firm"] == "Firm Practice"

    from celerp.services.peer_access import key_ids
    revoked = await real_client.post(f"/company-peers/grants/{key_ids(credential)[1]}/revoke",
                                     headers=auth(firm_tok))
    assert revoked.status_code == 200, revoked.text
    await _run(client_company, credential)

    lock = await _period_lock(real_client, tok)
    assert lock["firm_lock"] == {"date": "2026-03-31", "firm": "Second Practice"}, lock
    [row] = [r for r in await _received(real_client, tok) if r["id"] == local]
    assert row["reverse_on"] == MAY, row


async def test_an_expired_key_stops_binding_and_a_renewed_one_binds_again(
        real_client, real_engine, monkeypatch):
    """Firm A's key runs out: once the date the firm gave for it passes, its lock no
    longer binds, with no run needed. The firm renews
    the key and the next run brings June back, without moving the client's own lock,
    which it had moved back, a second time."""
    from celerp.models.company_peer import CompanyPeerGrant
    from celerp.services.peer_access import key_ids

    tok, firm_tok, client_company, credential, local, _ = await _may_journal_held_until_june(
        real_client, real_engine, monkeypatch)
    grant_id = key_ids(credential)[1]
    lapses = datetime.now(timezone.utc) + timedelta(seconds=3)
    async with maker(real_engine)() as s:  # the key's last days, as the firm reports them
        await s.execute(update(CompanyPeerGrant).where(CompanyPeerGrant.id == grant_id).values(
            expires_at=lapses))
        await s.commit()
    await _run(client_company, credential)
    assert (await _period_lock(real_client, tok))["firm_lock"]["date"] == "2026-06-30"
    await asyncio.sleep(max((lapses - datetime.now(timezone.utc)).total_seconds(), 0) + 0.5)
    assert (await _period_lock(real_client, tok))["firm_lock"] is None
    [row] = [r for r in await _received(real_client, tok) if r["id"] == local]
    assert row["reverse_on"] == MAY, row

    renewed = await real_client.post(f"/company-peers/grants/{grant_id}/renew", headers=auth(firm_tok))
    assert renewed.status_code == 200, renewed.text
    await _run(client_company, credential)
    lock = await _period_lock(real_client, tok)
    assert lock["firm_lock"] == {"date": "2026-06-30", "firm": "Firm Practice"}, lock
    assert lock["lock_date"] == "2026-01-31"
    notices = [body for _, body, _ in await _notices(real_engine, client_company)]
    assert len([b for b in notices if "Firm Practice" in b and "2026-06-30" in b]) == 1, notices


async def test_firm_locks_never_travel_in_a_company_copy(real_client, real_engine, monkeypatch):
    """The lock dates held per key are this company's record of its own accountants'
    keys, so a company copy never carries them."""
    import io
    import zipfile

    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    _route_to_app(monkeypatch, [])
    await _lock(real_client, firm_tok, "2026-03-31")
    await _run(client_company, credential)
    assert (await _period_lock(real_client, tok))["firm_lock"]["date"] == "2026-03-31"
    with zipfile.ZipFile(io.BytesIO(await download(real_client, tok))) as archive:
        text = "\n".join(archive.read(name).decode("utf-8", "replace") for name in archive.namelist()
                         if name.endswith(".json"))
    assert text and "firm_lock" not in text
