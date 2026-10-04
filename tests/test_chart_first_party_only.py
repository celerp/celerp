# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Only Celerp's own accounting module answers for the chart of accounts.

The journal boundary, the posting-account choices and adding an account for a role all
read the chart. A third-party module that fills slots named after them, and loads
before accounting, changes none of it: posting still checks the real chart, the
choices list the real accounts, and an added account lands in the real chart. Nor can
its own code hand core a chart.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from celerp.modules import loader, slots

_NAMES = ("journal_accounts", "chart_accounts", "add_chart_account")

_HOSTILE = '''
CALLS = []

async def lock(session, company_id, codes):
    CALLS.append("lock")
    return {c: {"code": c, "account_type": "asset", "is_active": True, "has_children": False}
            for c in codes}

async def chart(session, company_id):
    CALLS.append("chart")
    return [{"code": "666", "name": "Elsewhere", "account_type": "asset", "is_active": True,
             "has_children": False}]

async def add(session, company_id, *, code, name, account_type):
    CALLS.append("add")
'''


@pytest.fixture
def hostile(tmp_path, monkeypatch):
    """Install a third-party module filling the three names, its entries ahead of
    accounting's (a module whose name sorts first loads first). Returns its call log."""
    import importlib
    import sys

    before_path = list(sys.path)

    def install() -> list:
        name, inner = f"acme-{uuid.uuid4().hex[:8]}", f"acme_{uuid.uuid4().hex[:8]}"
        pkg = tmp_path / "modules" / name
        (pkg / inner).mkdir(parents=True)
        manifest = {"name": name, "version": "1.0.0", "slots": {
            "journal_accounts": [{"handler": f"{inner}.books:lock"}],
            "chart_accounts": [{"handler": f"{inner}.books:chart"}],
            "add_chart_account": [{"handler": f"{inner}.books:add"}],
        }}
        (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {manifest!r}\n")
        (pkg / inner / "__init__.py").write_text("")
        (pkg / inner / "books.py").write_text(_HOSTILE)
        monkeypatch.setenv("MODULE_DIR", str(pkg.parent))
        loader.load_all(str(pkg.parent), {name})
        assert loader.is_running(name), loader.load_errors()
        for slot in _NAMES:
            entries = slots.get(slot)
            slots._slots[slot] = ([e for e in entries if e.get("_module") == name]
                                  + [e for e in entries if e.get("_module") != name])
        return importlib.import_module(f"{inner}.books").CALLS

    yield install
    sys.path[:] = before_path


async def _register(client) -> uuid.UUID:
    r = await client.post("/auth/register", json={
        "company_name": "Books", "email": f"fp-{uuid.uuid4().hex[:8]}@chart.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    r = await client.get("/companies/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    return uuid.UUID(r.json()["id"])


async def _post(session, company_id, entries):
    from celerp.events.engine import emit_event

    key = uuid.uuid4().hex
    return await emit_event(
        session, company_id=company_id, entity_id=f"je:test:{key}", entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"ts": "2026-01-05", "memo": "Chart", "entries": entries},
        actor_id=None, location_id=None, source="test", idempotency_key=key, metadata_={},
    )


async def _ledger_rows(session, company_id) -> int:
    from celerp.models.ledger import LedgerEntry

    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == company_id))).scalar_one()


@pytest.mark.asyncio
async def test_posting_checks_the_real_chart_not_a_third_party_one(client, session, hostile):
    from sqlalchemy import update

    from celerp_accounting.models import Account
    from celerp.services.journal_accounts import lock_accounts

    cid = await _register(client)
    calls = hostile()
    held = await lock_accounts(session, cid, {"1120", "9999"})
    assert set(held) == {"1120"}
    before = await _ledger_rows(session, cid)
    # An account the chart does not hold stays refused.
    with pytest.raises(HTTPException) as exc:
        await _post(session, cid, [{"account": "9999", "debit": 5}, {"account": "1120", "credit": 5}])
    assert exc.value.status_code == 422 and "9999" in exc.value.detail
    # An account made inactive stays inactive to a payment.
    await session.execute(update(Account).where(
        Account.company_id == cid, Account.code == "1111").values(is_active=False))
    from celerp.services.journal_accounts import require_settlement_account
    with pytest.raises(HTTPException) as exc:
        await require_settlement_account(session, cid, "1111")
    assert "inactive" in exc.value.detail
    assert await _ledger_rows(session, cid) == before
    assert calls == []


@pytest.mark.asyncio
async def test_choices_list_and_add_accounts_in_the_real_chart(session, monkeypatch, hostile):
    from celerp_accounting.models import Account
    from celerp.services.posting_readiness import account_names, apply_choices, readiness
    from test_posting_roles_finalize import _CHOICES
    from test_posting_roles_migration_sinks import _import_chart, _staged_context

    context = await _staged_context(session)
    await _import_chart(context)
    calls = hostile()
    rows = await readiness(session, context.company_id)
    offered = {c["code"] for row in rows for c in row["candidates"]}
    assert "666" not in offered and "400" in offered
    assert (await account_names(session, context.company_id, ["400", "666"]))["666"] == "666"
    await apply_choices(session, context.company_id, _CHOICES)
    codes = set((await session.execute(select(Account.code).where(
        Account.company_id == context.company_id))).scalars())
    assert {"6950", "1111", "4300", "6970"} <= codes
    assert calls == []


@pytest.mark.asyncio
async def test_a_third_party_module_cannot_register_a_chart(tmp_path):
    import importlib.util

    from celerp.services import journal_accounts

    source = tmp_path / "acme-books" / "acme_books" / "books.py"
    source.parent.mkdir(parents=True)
    source.write_text(_HOSTILE)
    spec = importlib.util.spec_from_file_location("acme_books_forged", source)
    books = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(books)
    before = journal_accounts.chart_access()
    with pytest.raises(journal_accounts.UntrustedChartError):
        journal_accounts.register_chart(journal_accounts.ChartAccess(
            lock_accounts=books.lock, list_accounts=books.chart, add_account=books.add))
    assert journal_accounts.chart_access() is before is not None


@pytest.mark.asyncio
async def test_a_stopped_accounting_module_no_longer_answers_for_the_chart(client, session, monkeypatch):
    from celerp.services.journal_accounts import lock_accounts

    cid = await _register(client)
    assert await lock_accounts(session, cid, {"1120"}) is not None
    monkeypatch.setitem(loader._load_errors, "celerp-accounting", "UI setup failed")
    assert await lock_accounts(session, cid, {"1120"}) is None
