# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Client companies: every company the signed-in user can open, with its accountant sync,
last sync and what needs attention, and a way into each."""

from __future__ import annotations

from urllib.parse import urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.components.activity import relative_time
from ui.components.table import search_results
from ui.config import get_token as _token
from ui.i18n import get_lang, t

SYNC_STATES = ("on", "off", "failing", "expiring")
MOVE_PAGE = "/setup/new-company/migrate"


def _switch(company_id: str, destination: str | None = None) -> str:
    """Open the company, landing on *destination* (one of the switch destinations)."""
    return f"/switch-company/{company_id}" + (f"?{urlencode({'next': destination}, safe='/')}" if destination else "")


def _attention_link(row: dict, item: dict, lang: str) -> FT | str:
    if item.get("post_again"):  # a client's reversal: post the journal again, prefilled
        return A(t("firm_view.post_again", lang), href=_switch(row["company_id"], item["post_again"]),
                 cls="btn btn--secondary btn--xs")
    href = item.get("href")
    if not href:
        return ""
    if not href.startswith("/migrations/"):
        href = _switch(row["company_id"], href)
    return A(t("firm_view.go_there", lang), href=href, cls="btn btn--secondary btn--xs")


def _attention(row: dict, lang: str) -> FT:
    """What needs attention, opened in place; each item names its record and the reason."""
    items = row.get("attention") or []
    hints = [P(reason, cls="settings-hint") for reason in row.get("unavailable") or []]
    if not items:
        return Div(Span("--"), *hints)
    return Div(
        Details(
            Summary(t("firm_view.to_look_at", lang, n=len(items))),
            Ul(*[Li(
                Strong(t(f"firm_view.kind_{item['kind']}", lang)), " ",
                Span(item.get("label") or "--"), ": ", Span(item.get("reason") or ""), " ",
                _attention_link(row, item, lang),
            ) for item in items]),
        ),
        *hints,
    )


def _actions(row: dict, lang: str) -> FT:
    if row.get("role") is None:  # a move that has not finished opens from its own page
        [move] = [i for i in row["attention"] if i["kind"] == "migration"]
        return A(t("firm_view.continue_move", lang), href=move["href"], cls="btn btn--secondary btn--sm")
    cid = row["company_id"]
    return Div(
        A(t("firm_view.open", lang), href=_switch(cid), cls="btn btn--secondary btn--sm"),
        A(t("firm_view.send_copy", lang), href=_switch(cid, "/settings/company-handoff"),
          cls="btn btn--secondary btn--sm"),
        A(t("firm_view.invite", lang), href=_switch(cid, "/settings/users/new"),
          cls="btn btn--secondary btn--sm"),
        cls="flex-row gap-sm firm-view-actions",
    )


def _sync(row: dict, lang: str) -> str:
    return t(f"firm_view.sync_{row['sync']}", lang) if row.get("sync") else "--"


def _table(rows: list[dict], lang: str, params: dict, error: str | None = None) -> FT:
    """The companies, newest activity first; replaced in place when the filters change.
    ``params`` are the filters applied: with none, an empty list invites the first move."""
    notice = [P(error, cls="flash flash--warning")] if error else []
    if not rows:
        if params:  # the filters found nothing, or were refused: lead back to the whole list
            none = "firm_view.no_match" if params.get("q") else "firm_view.no_sync_match"
            body = [*([] if error else [P(t(none, lang))]),
                    A(t("acct.filter_clear", lang), href="/companies", cls="btn btn--secondary btn--sm")]
        elif error:
            body = []
        else:
            body = [P(t("firm_view.empty", lang)),
                    A(t("firm_view.move", lang), href=MOVE_PAGE, cls="btn btn--primary btn--sm")]
        return Div(*notice, *body, id="firm-view")
    return Div(
        *notice,
        Table(
            Thead(Tr(
                Th(t("th.company", lang)),
                Th(t("firm_view.sync", lang)),
                Th(t("firm_view.last_sync", lang)),
                Th(t("firm_view.attention", lang)),
                Th(t("th.actions", lang)),
            )),
            Tbody(*[Tr(
                Td(row["company_name"]),
                Td(_sync(row, lang)),
                Td(relative_time(row["last_sync"]) if row.get("last_sync") else "--"),
                Td(_attention(row, lang), cls="firm-view-attention"),
                Td(_actions(row, lang)),
            ) for row in rows]),
            cls="data-table",
        ),
        id="firm-view",
    )


def _filters(q: str, sync: str, lang: str) -> FT:
    """Search and the sync filter, both carried in the URL."""
    return Form(
        Input(type="search", name="q", value=q, placeholder=t("firm_view.search", lang),
              cls="search-input", id="firm-view-q", aria_label=t("firm_view.search", lang)),
        Select(
            Option(t("firm_view.sync_any", lang), value="", selected=not sync),
            *[Option(t(f"firm_view.sync_{state}", lang), value=state, selected=sync == state)
              for state in SYNC_STATES],
            name="sync", id="firm-view-sync", cls="cell-input cell-input--select",
            aria_label=t("firm_view.sync", lang),
        ),
        hx_get="/companies", hx_trigger="input changed delay:300ms from:#firm-view-q, change from:#firm-view-sync, submit",
        hx_target="#firm-view", hx_swap="outerHTML",
        onsubmit="return false", cls="flex-row gap-sm firm-view-filters",
    )


def setup_routes(app):

    @app.get("/companies")
    async def firm_view_page(request: Request):
        from ui.components.shell import base_shell, page_header
        from ui.components.table import breadcrumbs
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        q = (request.query_params.get("q") or "").strip()
        sync = request.query_params.get("sync") or ""
        params = {k: v for k, v in (("q", q), ("sync", sync)) if v}
        rows, error = [], None
        try:
            rows = (await api.firm_view(token, params)).get("items", [])
        except api.APIError as exc:
            error = str(exc.detail)
        table = _table(rows, lang, params, error)
        if request.headers.get("HX-Request"):
            return search_results(table, "/companies" + (f"?{urlencode(params)}" if params else ""))
        return await base_shell(
            breadcrumbs([(t("settings.your_companies", lang), "/settings/general?tab=company"),
                         (t("firm_view.title", lang), None)]),
            page_header(
                t("firm_view.title", lang),
                A(t("firm_view.move", lang), href=MOVE_PAGE, cls="btn btn--secondary btn--sm"),
            ),
            P(t("firm_view.description", lang)),
            _filters(q, sync, lang),
            table,
            title=t("firm_view.title", lang), nav_active="settings", request=request,
        )
