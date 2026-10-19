# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A representative sample of this round's newly-keyed refusals, across distinct
modules, actually renders in the user's language instead of the raw English main
wrote. One test per area: accounting posting, accounting chart, manufacturing,
tabular import, and inventory. Each calls the real production code path (not just
the catalog) and checks the Thai rendering differs from the English default and
names the same subject, while the English default itself names what to do next.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException

from ui.i18n import refusal_text, set_lang

AT = "2026-03-02T10:00:00+00:00"


@pytest.mark.asyncio
async def test_an_unknown_destination_account_is_refused_in_the_users_language(session, auth):
    """celerp/services/journal_accounts.py: prepare_journal_entry."""
    from celerp.services.journal_accounts import prepare_journal_entry

    with pytest.raises(HTTPException) as exc:
        await prepare_journal_entry(session, auth["company_id"],
                                     {"entries": [{"account": "NOPE999"}]})
    detail = exc.value.detail
    assert detail["message_key"] == "posting.destination.not_in_chart"
    assert detail["params"]["code"] == "NOPE999"
    en_text = refusal_text(detail)
    set_lang("th")
    try:
        th_text = refusal_text(detail)
    finally:
        set_lang("en")
    assert "NOPE999" in th_text
    assert th_text != en_text
    assert "chart of accounts" in en_text


@pytest.mark.asyncio
async def test_changing_a_deleted_account_is_refused_in_the_users_language(session, auth):
    """default_modules/celerp-accounting/celerp_accounting/chart_rules.py: change_account."""
    from celerp_accounting.chart_rules import change_account

    with pytest.raises(HTTPException) as exc:
        await change_account(session, auth["company_id"], "NOPE999", name="Whatever")
    detail = exc.value.detail
    assert detail["message_key"] == "chart.account_not_found"
    en_text = refusal_text(detail)
    set_lang("th")
    try:
        th_text = refusal_text(detail)
    finally:
        set_lang("en")
    assert th_text != en_text
    assert "Select Try again" in en_text


@pytest.mark.asyncio
async def test_issuing_to_an_unknown_manufacturing_order_is_refused_in_the_users_language(session, auth):
    """default_modules/celerp-manufacturing/celerp_manufacturing/movements.py: issue -> _run."""
    from celerp_manufacturing import movements

    with pytest.raises(HTTPException) as exc:
        await movements.issue(session, auth["company_id"], auth["user_id"], str(uuid.uuid4()),
                               None, None, at=AT)
    detail = exc.value.detail
    assert detail["message_key"] == "mfg.order_not_found"
    en_text = refusal_text(detail)
    set_lang("th")
    try:
        th_text = refusal_text(detail)
    finally:
        set_lang("en")
    assert th_text != en_text
    assert en_text == "Order not found."


def test_importing_an_empty_file_is_refused_in_the_users_language():
    """celerp/importers/tabular.py: read_table_at_header."""
    from celerp.importers.tabular import TabularError, read_table_at_header

    set_lang("en")
    try:
        with pytest.raises(TabularError) as exc:
            read_table_at_header(b"", "empty.csv")
        en_message = str(exc.value)
    finally:
        set_lang("en")
    assert "empty.csv" in en_message
    assert "Check you chose the right file" in en_message

    set_lang("th")
    try:
        with pytest.raises(TabularError) as exc:
            read_table_at_header(b"", "empty.csv")
        th_message = str(exc.value)
    finally:
        set_lang("en")
    assert th_message != en_message
    assert "empty.csv" in th_message


@pytest.mark.asyncio
async def test_undoing_a_merge_of_an_unknown_item_is_refused_in_the_users_language(client, auth):
    """default_modules/celerp-inventory/celerp_inventory/routes.py: undo_merge."""
    missing_id = str(uuid.uuid4())

    client.cookies.set("celerp_lang", "en")
    r = await client.post(f"/items/{missing_id}/undo-merge", headers=auth["headers"])
    assert r.status_code == 404, r.text
    en_text = r.json()["detail"]
    assert "Select Try again" in en_text

    client.cookies.set("celerp_lang", "th")
    r = await client.post(f"/items/{missing_id}/undo-merge", headers=auth["headers"])
    assert r.status_code == 404, r.text
    th_text = r.json()["detail"]
    assert th_text != en_text
