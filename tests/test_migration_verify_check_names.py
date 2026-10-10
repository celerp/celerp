# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The verify page names each check by what it checks, never by the source's internal id,
and shows every figure the way the rest of the app does: money at its currency's
decimals, counts and quantities as plain numbers (also a count of documents in one currency). In every language."""

from __future__ import annotations

from decimal import Decimal

import re

import pytest
from fasthtml.common import to_xml
from starlette.requests import Request

from fixtures.manager_io import specs
from fixtures.manager_io.encoder import write_manager_file
from migration_support import real_engine  # noqa: F401 - fixture

# Counts and quantities: a currency on a document count says which documents were counted,
# it is not the count's unit.
COUNTED = {"document_count", "document_status", "inventory_quantity"}
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


async def _verify_html(run, monkeypatch, lang: str) -> str:
    import ui.routes.migrations as pages
    from ui import i18n

    async def reconciliation(token, run_id):
        return run.reconciliation

    async def get_run(token, run_id):
        return {"id": str(run.id), "lock_date": None}

    async def posting_accounts(token, run_id):
        return {"roles": []}

    monkeypatch.setattr(pages.api, "migration_reconciliation", reconciliation)
    monkeypatch.setattr(pages.api, "get_migration_run", get_run)
    monkeypatch.setattr(pages.api, "migration_posting_accounts", posting_accounts)
    request = Request({"type": "http", "method": "GET", "path": f"/migrations/{run.id}/verify",
                       "query_string": b"", "headers": []})
    token = i18n._current_lang.set(lang)
    try:
        return to_xml(await pages._verify_page(request, str(run.id)))
    finally:
        i18n._current_lang.reset(token)


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_verify_checks_read_as_names_and_formatted_figures(real_engine, monkeypatch, tmp_path, lang):  # noqa: F811
    from test_migration_e2e import migrate

    from ui.components.activity import fmt_qty
    from ui.components.table import fmt_money
    from ui.i18n import t

    path = write_manager_file(tmp_path / "basic.manager", specs.basic_objects(), specs.basic_blobs())
    run, rejected = await migrate(real_engine, path.read_bytes(), path.name, {"mode": "full_history"},
                                  monkeypatch, tmp_path / "data")
    assert rejected == [] and run.status == "ready_to_finalize", run.error_summary
    rows = run.reconciliation["rows"]
    assert any(UUID_TEXT.search(row["key"]) for row in rows)  # the source does key its records by id

    page = await _verify_html(run, monkeypatch, lang)
    table = page[page.index("<table"):page.index("</table>")]
    assert not UUID_TEXT.search(table), UUID_TEXT.search(table).group()
    # Document checks name the document type in the reader's language, not its code.
    assert t("settings.doc_type_invoice", lang) in table
    assert "invoice:" not in table and ">invoice<" not in table
    assert any(row["check"] == "document_count" and row["currency"] for row in rows)
    for row in rows:
        if row["celerp"] is None:
            continue
        money = row["currency"] and row["check"] not in COUNTED
        # A credit-side balance shows as the amount it is.
        value = -Decimal(row["celerp"]) + 0 if row.get("credit_normal") else row["celerp"]
        shown = fmt_money(value, row["currency"]) if money else fmt_qty(value)
        assert f">{shown}</td>" in table, (row, shown)
