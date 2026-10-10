# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Any module may post journal entries, and adds the accounts it posts to through
``journal_accounts.add_account``: the chart checks the account as Settings does,
refusing a code in use, a type the reports cannot sign, and a blank code or name.
"""
from __future__ import annotations

import importlib
import sys
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from celerp.modules import loader
from test_chart_first_party_only import _register
from test_helpers import in_language

pytestmark = pytest.mark.asyncio


_KIOSK = '''
import uuid

from celerp.events.engine import emit_event
from celerp.services.journal_accounts import add_account


async def open_float(session, company_id, amount):
    """Add the kiosk's cash float account and move ``amount`` into it from the bank."""
    await add_account(session, company_id, "KIOSK-1", "Kiosk cash float", "asset")
    key = uuid.uuid4().hex
    await emit_event(
        session, company_id=company_id, entity_id=f"je:kiosk:{key}", entity_type="journal_entry",
        event_type="acc.journal_entry.created",
        data={"ts": "2026-01-05", "memo": "Kiosk float",
              "entries": [{"account": "KIOSK-1", "debit": amount},
                          {"account": "1120", "credit": amount}]},
        actor_id=None, location_id=None, source="kiosk", idempotency_key=key, metadata_={},
    )
    return f"je:kiosk:{key}"
'''


@pytest.fixture
def kiosk(tmp_path, monkeypatch):
    """A third-party module, loaded the way any installed module is."""
    before_path = list(sys.path)
    name, inner = f"kiosk-{uuid.uuid4().hex[:8]}", f"kiosk_{uuid.uuid4().hex[:8]}"
    pkg = tmp_path / "modules" / name
    (pkg / inner).mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"PLUGIN_MANIFEST = {{'name': {name!r}, 'version': '1.0.0'}}\n")
    (pkg / inner / "__init__.py").write_text("")
    (pkg / inner / "books.py").write_text(_KIOSK)
    monkeypatch.setenv("MODULE_DIR", str(pkg.parent))
    loader.load_all(str(pkg.parent), {name})
    assert loader.is_running(name), loader.load_errors()
    assert loader.first_party_owner(pkg / inner / "books.py") is None
    yield importlib.import_module(f"{inner}.books")
    sys.path[:] = before_path


async def test_a_module_adds_an_account_and_posts_a_balanced_entry_to_it(client, session, kiosk):
    from celerp.models.projections import Projection
    from celerp_accounting.models import Account

    cid = await _register(client)
    entity_id = await kiosk.open_float(session, cid, 250)
    await session.commit()

    account = (await session.execute(select(Account).where(
        Account.company_id == cid, Account.code == "KIOSK-1"))).scalar_one()
    assert (account.name, account.account_type, account.parent_code, account.is_active) == (
        "Kiosk cash float", "asset", None, True)
    je = await session.get(Projection, {"company_id": cid, "entity_id": entity_id})
    lines = {e["account"]: e for e in je.state["entries"]}
    assert lines["KIOSK-1"]["debit"] == 250 and lines["1120"]["credit"] == 250


@pytest.mark.parametrize(("code", "name", "account_type", "message"), [
    ("1120", "Second bank", "asset", "Account code 1120 is already used"),
    ("KIOSK-2", "Float", "money", "Account type must be one of: asset, liability, equity"),
    ("  ", "Float", "asset", "Account code is required."),
    ("KIOSK-2", " ", "asset", "Account name is required."),
    ("K" * 33, "Float", "asset", "Account code must be 32 characters or fewer."),
    (7, "Float", "asset", "Account code must be text."),
])
async def test_an_invalid_account_is_refused_with_a_plain_message(
        client, session, code, name, account_type, message):
    from celerp_accounting.models import Account
    from celerp.services.journal_accounts import add_account

    cid = await _register(client)
    before = set((await session.execute(select(Account.code).where(Account.company_id == cid))).scalars())
    with pytest.raises(HTTPException) as exc:
        await add_account(session, cid, code, name, account_type)
    detail = exc.value.detail
    assert exc.value.status_code in (409, 422) and detail["message"].startswith(message), detail
    assert in_language("en", detail) == detail["message"] and in_language("de", detail) != detail["message"], in_language("de", detail)
    await session.rollback()
    after = set((await session.execute(select(Account.code).where(Account.company_id == cid))).scalars())
    assert after == before


async def test_no_account_is_added_while_accounting_is_not_running(client, session, monkeypatch):
    from celerp.services.journal_accounts import add_account

    cid = await _register(client)
    monkeypatch.setitem(loader._load_errors, "celerp-accounting", "UI setup failed")
    with pytest.raises(HTTPException) as exc:
        await add_account(session, cid, "KIOSK-3", "Float", "asset")
    detail = exc.value.detail
    assert detail["message"] == ("Accounting is not running, so no account can be added "
                                 "to the chart of accounts.") == in_language("en", detail)
    assert in_language("de", detail) != detail["message"]
