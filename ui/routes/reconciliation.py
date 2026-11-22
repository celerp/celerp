# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A bank account's reconciliation history and the currency correction it offers; each
statement's lines are worked on the bank page (ui/routes/bank.py)."""

from __future__ import annotations

from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.bank_groups import bank_url, page_flashes, relabel_notice
from ui.components.shell import base_shell, no_access_banner, page_header, page_title
from ui.components.table import EMPTY, display_enum, fmt_money, status_cards
from ui.config import get_token as _token
from ui.i18n import t


# ── Reconciliation history ───────────────────────────────────────────────────

# Session statuses in the order the history filter cards show them, with their colour.
_SESSION_STATUSES = {"open": "yellow", "completed": "green"}


def _history_table(items: list[dict], bank_id: str, currency: str) -> FT:
    """An account's reconciliations, newest statement first (the API's order); each opens
    the bank page, where its lines are."""
    headers = (Th(t("label.statement_date")), Th(t("label.statement_closing_balance"), cls="cell--number"),
               Th(t("th.status")), Th(t("th.completed")))

    def _rows(i: dict) -> tuple[FT, ...]:
        row = Tr(
            Td(A(i["statement_date"], href=bank_url(bank_id) + "?view=all")),
            Td(fmt_money(i["statement_balance"], currency), cls="cell--number"),
            Td(display_enum(i["status"], "recon_session_status")),
            Td((i.get("completed_at") or "")[:10] or EMPTY),
        )
        if not i.get("needs_review"):
            return (row,)
        # The note spans the row below so it wraps instead of widening a column.
        return row, Tr(Td(P(t("recon.needs_review"), cls="flash flash--warning"),
                          colspan=str(len(headers)), cls="cell--note"))
    return Table(
        Thead(Tr(*headers)),
        Tbody(*[r for i in items for r in _rows(i)]),
        cls="data-table",
    )


async def _no_access_page(request: Request, error: APIError, title_key: str):
    """A reconciliation page the reader's role may not open: the missing permission and the
    way back, never a form it cannot submit."""
    return await base_shell(page_header(t(title_key)), no_access_banner(error),
                            title=page_title(title_key), nav_active="accounting", request=request)



# ── Setup routes ─────────────────────────────────────────────────────────────

def setup_routes(app):

    @app.get("/accounting/reconcile/history/{bank_id}")
    async def reconcile_history(request: Request, bank_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            bank = await api.get_bank_account(token, bank_id)
            items = (await api.get_bank_reconciliations(token, bank_id)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if e.status == 403:
                return await _no_access_page(request, e, "recon.history")
            return RedirectResponse(
                "/settings/accounting?tab=bank-accounts&msg=" + quote(str(e.detail), safe=""),
                status_code=302,
            )
        status = request.query_params.get("status", "")
        if status not in _SESSION_STATUSES:
            status = ""
        base = f"/accounting/reconcile/history/{bank_id}"
        reconcile = A(t("acct.reconcile"), href=bank_url(bank_id),
                      cls="btn btn--primary")
        cards = status_cards(
            [{"label": display_enum(s, "recon_session_status"),
              "count": sum(1 for i in items if i["status"] == s), "total": None, "status": s,
              "color": color} for s, color in _SESSION_STATUSES.items()],
            base, status or None)
        shown = [i for i in items if not status or i["status"] == status]
        return await base_shell(
            *page_flashes(request.query_params),
            page_header(f"{t('recon.history')}: {bank.get('bank_name', '')}",
                        A(t("btn.back"), href="/settings/accounting?tab=bank-accounts", cls="btn btn--secondary"),
                        reconcile),
            P(t("recon.history_intro"), cls="text-muted"),
            relabel_notice(bank, base),
            cards,
            _history_table(shown, bank_id, bank["currency"]) if items else Div(
                P(t("recon.history_empty"), cls="empty-state"), reconcile, cls="settings-card"),
            title=page_title("recon.history"),
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/reconcile/relabel/{bank_id}")
    async def relabel_bank(request: Request, bank_id: str):
        """Set an account to the books' currency (``_relabel_notice``), then return to the
        page the notice was on, saying what changed and how to change it back."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        back = str((await request.form()).get("next", ""))
        if not back.startswith("/accounting/reconcile/") or "//" in back or "\\" in back:
            back = f"/accounting/reconcile/history/{bank_id}"
        try:
            bank = await api.get_bank_account(token, bank_id)
            base = bank.get("relabel_to")
            if base:
                await api.patch_bank_account(token, bank_id, {"currency": base})
                done = t("recon.relabel_done", currency=bank["currency"], base=base)
                return RedirectResponse(back + "?done=" + quote(done, safe=""), status_code=303)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return RedirectResponse(back + "?msg=" + quote(str(e.detail), safe=""), status_code=303)
        return RedirectResponse(back, status_code=303)

