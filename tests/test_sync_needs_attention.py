# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A sync that finished but left records waiting on a person says Needs attention, with
the count, on the client's connector page and on the firm's companies page; the record
still counts as processed, so the sync's result and the other entities are unchanged.
Once the record is resolved and synced again, both screens are back to OK."""

from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401
from test_company_handoff_identity_restore import _link_verified  # noqa: F401
from test_peer_sync_end_to_end import FIRM, _route_to_app
from test_peer_two_way_sync import ADJUSTED, _from_firm, _journals, _lock, _pair_journals, _post_journal
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def _sync(client_company: str, credential: str):
    """One run of the connector through the runner, as Sync now does."""
    from celerp.connectors.base import ConnectorContext, SyncDirection
    from celerp.connectors.celerp import CelerpConnector
    from celerp.connectors.sync_runner import run_connector_sync

    ctx = ConnectorContext(company_id=client_company, store_handle=FIRM, access_token=credential)
    return await run_connector_sync(CelerpConnector(), ctx, SyncDirection.BOTH)


async def _client_screen(client_company: str) -> tuple[dict, str, str, list[dict]]:
    """The client's Celerp connector: the latest runs, the status table with its
    attention list, the card's last-sync line, and the attention list itself."""
    from ui.routes import settings_connectors as page

    runs = await page._entity_runs(client_company, "celerp")
    attention = await page._attention(client_company, "celerp")
    view = to_xml(page._connector_status_view("celerp", runs, attention=attention))
    latest = max(runs.values(), key=lambda r: r.started_at)
    card = to_xml(page._last_sync_info(latest, page._open_attention_count(attention),
                                       href="/settings/connectors/celerp"))
    return runs, view, card, attention


def _toast(runs: dict, attention: list[dict]) -> tuple[str, str]:
    """The toast the status poll shows when the sync has finished."""
    from ui.routes import settings_connectors as page

    return page._sync_complete_toast(runs, attention, "Celerp")


async def _firm_screen(real_client, firm_tok: str) -> tuple[dict, str]:
    from ui.routes.firm_view import _table

    r = await real_client.get("/auth/firm-view", headers=auth(firm_tok))
    assert r.status_code == 200, r.text
    [row] = [row for row in r.json()["items"] if row["company_name"] == "Demo Trading"]
    return row, to_xml(_table([row], "en", filtered=False))


def _journals_row(view: str) -> str:
    start = view.index(t("connectors.entity_journals"))
    return view[start:view.index("</tr>", start)]


async def test_a_declined_journal_shows_needs_attention_until_it_posts(real_client, real_engine, monkeypatch):
    tok, firm_tok, client_company, credential, _ = await _pair_journals(real_client, real_engine)
    await _post_journal(real_client, firm_tok, ADJUSTED, 80.0, "Year-end accrual")
    await _lock(real_client, tok, "2026-06-30")
    _route_to_app(monkeypatch, [])

    results = await _sync(client_company, credential)
    # The decline is processed, not an error: the result and the other entities stand.
    assert all(r.ok for r in results), [(r.entity, r.errors) for r in results]
    assert _from_firm(await _journals(real_engine, client_company)) == {}

    runs, view, card, attention = await _client_screen(client_company)
    assert runs["journals"].status == "success"
    assert {e: r.status for e, r in runs.items()} == {e: "success" for e in runs}
    needs = t("connectors.status_needs_attention", n=1)
    row = _journals_row(view)
    assert needs in row and t("connectors.status_ok") not in row, row
    assert "Year-end accrual" in view and "2026-06-30" in view
    others = [e for e in runs if e != "journals"]
    assert others and view.count(f">{t('connectors.status_ok')}<") == len(others), view
    assert "connector-sync-info--warn" in card and t("connectors.needs_attention") in card, card
    assert _toast(runs, attention) == (t("connectors.sync_complete_attention", n=1), "warning")

    firm_row, firm_html = await _firm_screen(real_client, firm_tok)
    assert [i["kind"] for i in firm_row["attention"]] == ["declined"], firm_row["attention"]
    assert t("firm_view.to_look_at", n=1) in firm_html
    assert t("firm_view.kind_declined") in firm_html and "Year-end accrual" in firm_html

    # Resolved: the client lifts its lock and syncs again; the journal posts once.
    await _lock(real_client, tok, None)
    results = await _sync(client_company, credential)
    assert all(r.ok for r in results), [(r.entity, r.errors) for r in results]
    [posted] = _from_firm(await _journals(real_engine, client_company)).values()
    assert posted["status"] == "posted" and posted["memo"] == "Year-end accrual"

    runs, view, card, attention = await _client_screen(client_company)
    row = _journals_row(view)
    assert t("connectors.status_ok") in row and t("connectors.needs_attention") not in view, row
    assert "connector-sync-info--ok" in card and t("connectors.needs_attention") not in card, card
    assert _toast(runs, attention) == (t("connectors.sync_complete"), "success")

    firm_row, firm_html = await _firm_screen(real_client, firm_tok)
    assert [i for i in firm_row["attention"] if i["kind"] == "declined"] == []
    assert t("firm_view.kind_declined") not in firm_html


async def test_a_failed_sync_still_says_failed(real_client, real_engine, monkeypatch):
    """The guard: Needs attention never covers a failure; a run with errors keeps its
    Issues or Failed status and the error toast."""
    from types import SimpleNamespace
    from datetime import datetime, timezone

    from ui.routes import settings_connectors as page

    def run(status, errors=None):
        return SimpleNamespace(status=status, errors=errors, finished_at=datetime.now(timezone.utc),
                               started_at=datetime.now(timezone.utc), created_count=0, updated_count=0)
    entry = [{"id": "x", "label": "Year-end accrual", "reason": "locked", "entity": "journals"}]
    runs = {"journals": run("failed", ["Journal entry fetch incomplete"]), "contacts": run("success")}
    msg, kind = page._sync_complete_toast(runs, entry, "Celerp")
    assert kind == "error" and msg == t("connectors.sync_failed", service="Celerp")
    table = to_xml(page._entity_status_table(runs, attention={"journals": 1}))
    assert t("connectors.status_failed") in table and t("connectors.needs_attention") not in table
    card = to_xml(page._last_sync_info(run("partial", ["x"]), 1))
    assert "connector-sync-info--warn" in card and page._sync_status_label("partial") in card
    assert t("connectors.needs_attention") not in card
