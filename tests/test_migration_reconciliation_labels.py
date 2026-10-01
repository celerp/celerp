# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Reconciliation rows keep their machine identity and carry the source's own names.

Each row is keyed by (measure, key, currency) for matching and for the technical pack.
For display a row also carries the name the source gives the record it is about
(account, contact, item) and whether a credit balance is the figure's normal side."""

from __future__ import annotations

import re

import pytest

from celerp.importers.adapters.manager_io.book import read_book
from celerp.importers.adapters.manager_io.sqlite_reader import ManagerReader
from celerp.importers.schema import ReconciliationMeasure as M
from fixtures.manager_io.support import adapter, artifact, ref
from migration_support import real_client, real_engine  # noqa: F401 - fixtures
from test_migration_inventory_provenance import INVENTORY, MODES, _migrated
from test_migration_manager_cogs import _decisions

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
RECORD_MEASURES = {M.TRIAL_BALANCE, M.AR_CONTROL, M.AP_CONTROL, M.TAX_CONTROL, M.BANK_CASH,
                   M.AR_BY_CUSTOMER, M.AP_BY_SUPPLIER, M.INVENTORY_QUANTITY, M.INVENTORY_VALUE}
AP = ref("@BalanceSheetAccountsPayableAccount")
AR = ref("@BalanceSheetAccountsReceivableAccount")


def _book():
    with ManagerReader(INVENTORY) as reader:
        return read_book(reader)


@pytest.mark.parametrize("decisions", MODES)
def test_every_record_row_is_labelled_with_the_source_name(decisions):
    """RED before the change: expectations carry only the source key."""
    book = _book()
    rows = adapter().source_expectations([artifact(INVENTORY)], _decisions(decisions)).expectations
    names = {**{k: (f"{a.code} {a.name}" if a.code else a.name) for k, a in book.accounts.items()},
             **{k: c.name for k, c in book.contacts.items()},
             **{k: (f"{i.name} ({i.code})" if i.code else i.name) for k, i in book.items.items()}}
    for row in rows:
        if row.measure in RECORD_MEASURES:
            assert row.label == names[row.key], (row.measure, row.key)
            assert not UUID.search(row.label)
        else:
            assert row.label == ""


@pytest.mark.parametrize("decisions", MODES)
def test_credit_side_figures_are_marked_credit_normal(decisions):
    book = _book()
    rows = adapter().source_expectations([artifact(INVENTORY)], _decisions(decisions)).expectations
    by = {(r.measure, r.key): r for r in rows}
    assert by[(M.AP_CONTROL, AP)].credit_normal
    assert not by[(M.AR_CONTROL, AR)].credit_normal
    for row in rows:
        if row.measure == M.TRIAL_BALANCE:
            assert row.credit_normal == (book.accounts[row.key].account_type in ("liability", "equity", "revenue"))
        if row.measure in (M.AP_BY_SUPPLIER, M.TAX_CONTROL):
            assert row.credit_normal
        if row.measure in (M.AR_BY_CUSTOMER, M.INVENTORY_VALUE, M.INVENTORY_QUANTITY, M.DOCUMENT_COUNT):
            assert not row.credit_normal


@pytest.mark.asyncio
async def test_stored_report_keeps_identity_and_display_metadata(real_engine, monkeypatch, tmp_path):  # noqa: F811
    books = await _migrated(real_engine, monkeypatch, tmp_path)
    rows = books.run.reconciliation["rows"]
    ap = next(r for r in rows if r["check"] == "ap_control")
    assert ap["key"] == AP
    assert ap["label"] == "Accounts payable"
    assert ap["credit_normal"] is True
    assert all("label" in r and "credit_normal" in r for r in rows)
