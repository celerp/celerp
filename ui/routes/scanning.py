# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api

logger = logging.getLogger(__name__)
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.components.table import EMPTY, format_value
from ui.config import get_token as _token
from ui.i18n import t, get_lang

_SCAN_HISTORY_KEY = "celerp_scan_history"


def _resolve_result(result: dict) -> FT:
    entity_type = result.get("entity_type", "unknown")
    state = result.get("state", {})
    entity_id = result.get("entity_id", "")
    actions = result.get("available_actions", [])

    detail_link = ""
    if entity_type == "item":
        detail_link = f"/inventory/{entity_id}"
    elif entity_type == "doc":
        detail_link = f"/docs/{entity_id}"

    fields = []
    for key in ("sku", "name", "quantity", "status", "category", "barcode"):
        if state.get(key) is not None:
            fields.append(Div(
                Span(f"{key.replace('_', ' ').title()}: ", cls="detail-label"),
                Span(format_value(state[key]), cls="detail-value"),
            ))

    return Div(
        Div(
            Span(t("inv.found"), cls="detail-label"),
            Span(entity_type.replace("_", " ").title(), cls="badge badge--active"),
            A(t("inv._view_detail"), href=detail_link, cls="link") if detail_link else Span(),
            cls="scan-result-header",
        ),
        *fields,
        Div(
            *[Span(a.replace("_", " ").title(), cls="badge badge--draft") for a in actions],
            cls="scan-actions",
        ),
        cls="scan-result scan-result--found",
        id="scan-result",
    )


def _scan_input_panel(batch_id: str | None = None) -> FT:
    return Div(
        Form(
            Div(
                Input(
                    type="text",
                    name="code",
                    id="scan-input",
                    placeholder="Scan barcode or enter SKU…",
                    autofocus=True,
                    autocomplete="off",
                    cls="scan-code-input",
                ),
                Button(t("btn.lookup"), cls="btn btn--primary", type="submit"),
                cls="scan-input-row",
            ),
            Input(type="hidden", name="batch_id", value=batch_id or ""),
            hx_post="/scanning/scan",
            hx_target="#scan-result",
            hx_swap="outerHTML",
            hx_on__after_request="document.getElementById('scan-input').value=''; document.getElementById('scan-input').focus();",
        ),
        Div(id="scan-result", cls="scan-result scan-result--empty"),
        cls="scan-input-panel",
    )


def _batch_panel(batch_id: str | None) -> FT:
    if batch_id:
        return Div(
            Span(t("inv.active_batch"), cls="detail-label"),
            Span(batch_id.split(":")[-1][:12], cls="badge badge--active"),
            Form(
                Button(t("btn.complete_batch"), cls="btn btn--secondary btn--xs", type="submit"),
                hx_post=f"/scanning/batch/{batch_id}/complete",
                hx_target="#batch-panel",
                hx_swap="outerHTML",
            ),
            id="batch-panel",
            cls="batch-panel batch-panel--active",
        )
    return Div(
        Span(t("inv.no_active_batch"), cls="detail-label"),
        Form(
            Button(t("btn.start_batch"), cls="btn btn--secondary btn--xs", type="submit"),
            hx_post="/scanning/batch/start",
            hx_target="#batch-panel",
            hx_swap="outerHTML",
        ),
        id="batch-panel",
        cls="batch-panel",
    )


def _history_panel() -> FT:
    return Div(
        H3(t("page.recent_scans")),
        Div(id="scan-history", cls="scan-history"),
        Script("""
(function(){
  var KEY = 'celerp_scan_history';
  var hist = [];
  try { hist = JSON.parse(localStorage.getItem(KEY) || '[]'); } catch(e) {}
  var el = document.getElementById('scan-history');
  if (!el) return;
  if (!hist.length) { el.innerHTML = '<p class="empty-state">No recent scans</p>'; return; }
  el.innerHTML = hist.slice(0, 20).map(function(s) {
    return '<div class="scan-history-item"><span class="scan-code">' + s.code + '</span>'
      + '<span class="scan-time">' + s.time + '</span>'
      + (s.found ? '<span class="badge badge--active">found</span>' : '<span class="badge badge--draft">not found</span>')
      + '</div>';
  }).join('');
})();
"""),
        cls="history-panel",
    )


def setup_routes(app):
    # Scanning module disabled until properly finished — all routes return 404
    from starlette.responses import Response as _Response

    @app.get("/scanning")
    async def scanning_page(request: Request):
        return _Response(status_code=404)

    @app.post("/scanning/scan")
    async def do_scan(request: Request):
        return _Response(status_code=404)

    @app.post("/scanning/batch/start")
    async def start_scan_batch(request: Request):
        return _Response(status_code=404)

    @app.post("/scanning/batch/{batch_id:path}/complete")
    async def complete_scan_batch(request: Request, batch_id: str):
        return _Response(status_code=404)
