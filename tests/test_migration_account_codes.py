# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""An imported chart keeps the source's account codes, banks included, and a clearing
account stays an ordinary asset account rather than becoming a bank."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from test_migration_sinks import _no_attachments, _persist_mappings, _sink_context

_PROVENANCE = {"source_system": "odoo"}


@pytest.mark.asyncio
async def test_bank_and_clearing_accounts_keep_their_source_codes(client, session):
    from celerp.importers.schema import CIFAccount
    from celerp.importers.sinks import sink_for
    from celerp_accounting.models import Account, BankAccount

    _headers, context = await _sink_context(client, session, _no_attachments)
    accounts = [
        CIFAccount(**_PROVENANCE, source_type="Account", source_external_id="a-bank", code="101000",
                   name="Bank", account_type="asset", control="bank"),
        CIFAccount(**_PROVENANCE, source_type="Account", source_external_id="a-cash", code="101501",
                   name="Cash", account_type="asset", control="cash"),
        CIFAccount(**_PROVENANCE, source_type="Account", source_external_id="a-out", code="101402",
                   name="Outstanding Receipts", account_type="asset", control="clearing"),
    ]
    result = await sink_for("accounts").import_batch(context, accounts)
    await _persist_mappings(context, result)
    assert result.errors == []
    assert {m.source_external_id: m.target_entity_id for m in result.mappings} == {
        "a-bank": "101000", "a-cash": "101501", "a-out": "101402",
    }
    banks = set((await session.execute(select(BankAccount.chart_account_code).where(
        BankAccount.company_id == context.company_id))).scalars())
    assert {"101000", "101501"} <= banks and "101402" not in banks
    clearing = (await session.execute(select(Account).where(
        Account.company_id == context.company_id, Account.code == "101402"))).scalar_one()
    assert clearing.account_type == "asset"
