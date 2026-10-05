# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A release that adds a posting role never blocks an older company or backup.

A company (or a restored backup) written under an earlier schema opens and boots
normally even though it has never heard of a role a later release added. Startup
maps the new role onto its seeded account only when that account provably exists,
active and of the right type; otherwise the role is left unmapped, shown as such in
Settings, and only the one operation that actually needs it is refused. Everything
else keeps posting. A settings blob written by a release newer than this one, naming
a role this code does not know, is read without crashing.

A restored copy carries its posting roles and its whole chart from the backup file
itself: provisioning a new company from a backup never reseeds the default chart, so
the copy posts on its own accounts whether or not the chart-seeding module is even
available at restore time.
"""
from __future__ import annotations

import pytest
from sqlalchemy import update

from celerp.accounting_roles import (
    POSTING_ACCOUNTS_PATH,
    POSTING_ROLES_SCHEMA,
    ROLES_KEY,
    SCHEMA_KEY,
    SCOPES_KEY,
)
from celerp.services.company_lock import locked_company
from migration_support import real_client, real_engine  # noqa: F401  (fixtures)
from test_cost_restatement import auth, ids  # noqa: F401  (auth and ids are fixtures)
from test_posting_accounts_panel import _panel, _row
from test_posting_roles_autoje import _invoice, _pay
from test_posting_roles_copy import _POSTING_KEYS, _accounts, _settings
from test_posting_roles_rollout import _startup


async def _simulate_older_schema(session, company_id, *, drop: tuple[str, ...], schema: int = 0) -> None:
    """Rewrite the company's settings as an earlier release would have left them:
    lower schema number, and ``drop`` missing from both the role map and its scopes."""
    company = await locked_company(session, company_id)
    roles = {k: v for k, v in company.settings[ROLES_KEY].items() if k not in drop}
    scopes = {k: v for k, v in company.settings[SCOPES_KEY].items() if k not in drop}
    company.settings = {**company.settings, SCHEMA_KEY: schema, ROLES_KEY: roles, SCOPES_KEY: scopes}
    await session.commit()


@pytest.mark.asyncio
async def test_a_role_missing_from_an_older_schema_is_mapped_when_its_seeded_target_exists(session, auth):
    cid = auth["company_id"]
    await _simulate_older_schema(session, cid, drop=("fx_gain",))

    await _startup(session)

    settings = (await locked_company(session, cid)).settings
    assert settings[SCHEMA_KEY] == POSTING_ROLES_SCHEMA
    assert settings[ROLES_KEY]["fx_gain"] == "6960"
    assert "6960" in settings[SCOPES_KEY]["fx_gain"]


@pytest.mark.asyncio
async def test_a_role_whose_seeded_target_is_gone_stays_unmapped_and_only_its_own_operation_fails(
        session, client, auth):
    from celerp_accounting.models import Account

    cid = auth["company_id"]
    await _simulate_older_schema(session, cid, drop=("fx_gain",))
    await session.execute(update(Account).where(Account.company_id == cid, Account.code == "6960")
                          .values(is_active=False))
    await session.commit()

    await _startup(session)
    settings = (await locked_company(session, cid)).settings
    assert "fx_gain" not in settings[ROLES_KEY]

    # An operation that needs nothing fx-related still posts.
    ordinary = await _invoice(client, auth, 50.0)
    assert ordinary

    # Raising the first fx-touching document is what makes the fx roles required at
    # all; finalizing it needs no exchange-difference role, only settling it at a
    # different rate does.
    gain_doc = await _invoice(client, auth, 100.0, currency="EUR", conversion_rate=1.1)

    panel = await _panel(client, auth)
    row = _row(panel, "fx_gain")
    assert row["required"] is True
    assert row["code"] is None
    assert row["status"] == "missing"

    r = await _pay(client, auth, gain_doc, 100.0, conversion_rate=1.2)
    assert r.status_code == 409, r.text
    assert "exchange gain" in r.json()["detail"]["message"].lower()
    assert r.headers["X-Celerp-Fix"] == POSTING_ACCOUNTS_PATH


@pytest.mark.asyncio
async def test_a_role_key_unknown_to_this_release_is_read_without_crashing(session, client, auth):
    """Settings written by a release newer than this one may carry a role this code has
    never heard of. Reading them (startup, the panel, an ordinary posting) is safe: the
    unknown key is neither interpreted nor dropped."""
    cid = auth["company_id"]
    company = await locked_company(session, cid)
    roles = {**company.settings[ROLES_KEY], "future_clearing_role": "9999"}
    scopes = {**company.settings[SCOPES_KEY], "future_clearing_role": ["9999"]}
    company.settings = {**company.settings, ROLES_KEY: roles, SCOPES_KEY: scopes}
    await session.commit()

    await _startup(session)

    panel = await _panel(client, auth)
    assert "future_clearing_role" not in {row["role"] for row in panel["roles"]}

    doc = await _invoice(client, auth, 25.0)
    assert doc

    settings = (await locked_company(session, cid)).settings
    assert settings[ROLES_KEY]["future_clearing_role"] == "9999"


@pytest.mark.asyncio
async def test_a_restored_copy_posts_on_its_own_accounts_without_the_chart_seeding_module(
        real_engine, real_client, tmp_path, monkeypatch):
    from company_backup_support import company, download, owner, restore, token
    from migration_support import maker
    from test_helpers import provision_company_books
    from test_posting_roles_copy import _invoice as _copy_invoice
    from test_posting_roles_copy import _post
    from test_restored_connector_safety import _local

    _local(monkeypatch, tmp_path)

    user = await owner(real_engine)
    source = await company(real_engine, user, "Pack Co", "pack-marker", settings={"currency": "USD"})
    async with maker(real_engine)() as s:
        await provision_company_books(s, source)
        await s.commit()
    tok = await token(real_engine, user, source)

    old_invoice = await _copy_invoice(real_client, tok, 80.0)

    # The chart-seeding module is only unavailable from here on: restoring a backup must
    # never need it, even though provisioning the source company just did.
    def _unavailable(*_a, **_k):
        raise AssertionError("chart seeding must not run while restoring a company backup")

    monkeypatch.setattr("celerp_accounting.routes.seed_chart_of_accounts", _unavailable)
    monkeypatch.setattr("celerp_accounting.routes._seed_default_bank_account", _unavailable)

    r = await restore(real_client, tok, await download(real_client, tok), mode="new_company")
    assert r.status_code == 201, r.text
    copy, copy_tok = r.json()["company_id"], r.json()["access_token"]
    assert str(copy) != str(source)

    before = {k: (await _settings(real_engine, source))[k] for k in _POSTING_KEYS}
    after = {k: (await _settings(real_engine, copy))[k] for k in _POSTING_KEYS}
    assert after == before
    assert await _accounts(real_engine, copy) == await _accounts(real_engine, source)

    await _post(real_client, copy_tok, f"/docs/{old_invoice}/payment", {
        "amount": 80.0, "payment_date": "2026-03-02", "bank_account": "1111"})

    new_invoice = await _copy_invoice(real_client, copy_tok, 30.0)
    await _post(real_client, copy_tok, f"/docs/{new_invoice}/payment", {
        "amount": 30.0, "payment_date": "2026-03-02", "bank_account": "1111"})
