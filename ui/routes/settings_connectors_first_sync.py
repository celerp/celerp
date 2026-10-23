# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Web Access: a connection's first sync.

Connecting never syncs. The connection's one next step is this preview: what the first
sync would do in the directions chosen, read from both sides without writing to either,
then an explicit Start. After it runs, the connector page shows what it did and how to
undo it.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from ui.components.activity import fmt_price, relative_time
from ui.i18n import get_lang, refusal_text, t

from celerp.connectors.registry import get as get_connector

# The preview's tables, in the order its tabs show them.
_SHOW = ("matches", "new", "no-sku", "problems", "publish", "orders")
_SHOW_KEY = {"matches": "matches", "new": "new", "no-sku": "no_sku", "problems": "problems",
             "publish": "to_publish", "orders": "orders"}


def _cell(value) -> str:
    from ui.routes.settings_connectors import _cell as cell
    return cell(value)


def _when(value) -> str:
    return relative_time(value) if value else "--"


def _preview_url(platform: str, show: str, orders_from: str) -> str:
    from urllib.parse import urlencode

    params = {"show": show, **({"orders_from": orders_from} if orders_from else {})}
    return f"/settings/connectors/{platform}/first-sync?{urlencode(params)}"


def _table(headers: list[tuple[str, bool]], rows: list[list], lang: str) -> FT:
    """A data table; a numeric column's header sits right, over its figures."""
    if not rows:
        return P(t("connectors.preview_none", lang), cls="empty-state-msg")
    def number(numeric: bool) -> dict:
        return {"cls": "cell--number"} if numeric else {}

    return Table(
        Thead(Tr(*[Th(label, **number(numeric)) for label, numeric in headers])),
        Tbody(*[Tr(*[Td(value, **number(numeric)) for value, (_, numeric) in zip(row, headers)],
                   cls="data-row") for row in rows]),
        cls="data-table",
    )


def _differences(differences: dict, lang: str) -> str:
    if not differences:
        return "--"
    return "; ".join(
        t("connectors.preview_difference", lang, field=t(f"connectors.conflict_field_{key}", lang),
          celerp=_cell(both[0]), remote=_cell(both[1]))
        for key, both in differences.items()
    )


def _section(platform: str, preview: dict, show: str, lang: str) -> FT:
    service = get_connector(platform).display_name
    directions = preview.get("directions") or {}
    products_in = directions.get("products") in ("inbound", "both")
    if show == "matches":
        hint = t("connectors.preview_matches_hint", lang, service=service)
        table = _table(
            [(t("connectors.preview_sku", lang), False), (t("connectors.preview_celerp_name", lang), False),
             (t("connectors.preview_store_name", lang, service=service), False),
             (t("connectors.preview_differences", lang), False),
             (t("connectors.preview_changed", lang, service=service), False)],
            [[_cell(r.get("sku")), _cell(r.get("celerp_name")), _cell(r.get("name")),
              _differences(r.get("differences") or {}, lang), _when(r.get("modified"))]
             for r in preview["matches"]], lang)
    elif show == "new":
        hint = t("connectors.preview_new_created" if products_in else "connectors.preview_new_left",
                 lang, service=service)
        table = _table(
            [(t("connectors.preview_sku", lang), False), (t("connectors.preview_store_name", lang, service=service), False),
             (t("connectors.preview_price", lang), True), (t("connectors.preview_changed", lang, service=service), False)],
            [[_cell(r.get("sku")), _cell(r.get("name")), _cell(fmt_price(r.get("price"), "sale_price")),
              _when(r.get("modified"))] for r in preview["new"]], lang)
    elif show == "no-sku":
        hint = t("connectors.attention_no_sku", lang, service=service)
        table = _table(
            [(t("connectors.preview_store_name", lang, service=service), False),
             (t("connectors.preview_changed", lang, service=service), False)],
            [[_cell(r.get("name")), _when(r.get("modified"))] for r in preview["no_sku"]], lang)
    elif show == "problems":
        hint = t("connectors.preview_problems_hint", lang)
        table = _table(
            [(t("connectors.preview_sku", lang), False), (t("connectors.preview_store_name", lang, service=service), False),
             (t("connectors.preview_reason", lang), False)],
            [[_cell(r.get("sku")), _cell(r.get("name")), _cell(r.get("reason"))]
             for r in preview["problems"]], lang)
    elif show == "publish":
        hint = t("connectors.preview_publish_hint", lang, service=service)
        table = _table(
            [(t("connectors.preview_sku", lang), False), (t("connectors.preview_celerp_name", lang), False),
             (t("connectors.preview_celerp_changed", lang), False)],
            [[_cell(r.get("sku")), _cell(r.get("name")), _when(r.get("modified"))]
             for r in preview["to_publish"]], lang)
    else:
        hint = preview.get("order_effect") or ""
        table = _table(
            [(t("connectors.preview_order", lang), False), (t("connectors.status", lang), False),
             (t("connectors.preview_placed", lang), False), (t("connectors.preview_total", lang), True),
             (t("connectors.preview_effect", lang), False)],
            [[_cell(r.get("number")), _cell(r.get("status")), _when(r.get("created")),
              _cell(fmt_price(r.get("total"), None, r.get("currency"))),
              t("connectors.preview_issued", lang) if r.get("posts") else t("connectors.preview_recorded", lang)]
             for r in preview["orders"]], lang)
    return Div(P(hint, cls="settings-hint"), table, cls="first-sync-section")


def _available(preview: dict) -> list[str]:
    directions = preview.get("directions") or {}
    shown = []
    if directions.get("products", "off") != "off":
        shown += ["matches", "new", "no-sku", "problems"]
        if directions.get("products") in ("outbound", "both"):
            shown.append("publish")
    if directions.get("orders", "off") != "off":
        shown.append("orders")
    return [s for s in _SHOW if s in shown]


def _preview_body(platform: str, preview: dict, show: str, orders_from: str, lang: str) -> FT:
    """The preview's counts as tabs, the table the URL names, and the order date."""
    tabs = _available(preview)
    if show not in tabs:
        show = next((s for s in tabs if preview[_SHOW_KEY[s]]), tabs[0] if tabs else "")
    nav = Div(*[
        A(t(f"connectors.preview_tab_{s.replace('-', '_')}", lang, n=len(preview[_SHOW_KEY[s]])),
          href=_preview_url(platform, s, orders_from),
          cls=f"tab {'tab--active' if s == show else ''}")
        for s in tabs
    ], cls="settings-tabs")
    dated = Form(
        Label(t("connectors.preview_orders_from", lang), For="first-sync-orders-from"),
        Input(type="date", name="orders_from", id="first-sync-orders-from", value=orders_from, cls="input input--sm"),
        Input(type="hidden", name="show", value=show),
        Button(t("connectors.preview_apply", lang), type="submit", cls="btn btn--sm btn--outline"),
        P(t("connectors.preview_orders_from_hint", lang), cls="settings-hint"),
        method="get", action=f"/settings/connectors/{platform}/first-sync", cls="first-sync-date",
    ) if "orders" in tabs else Span()
    return Div(
        dated,
        P(preview.get("order_effect") or "", cls="flash flash--info") if "orders" in tabs else Span(),
        nav,
        _section(platform, preview, show, lang) if show else P(t("connectors.preview_nothing", lang)),
        id=f"first-sync-preview-{platform}",
        hx_get=_preview_url(platform, show, orders_from) + "&fragment=1",
        hx_trigger="connectorDirectionsChanged from:body",
        hx_swap="outerHTML",
    )


def _start_form(platform: str, orders_from: str, lang: str) -> FT:
    service = get_connector(platform).display_name
    return Form(
        P(t("connectors.preview_match_title", lang), cls="settings-section-title"),
        Label(Input(type="radio", name="match", value="keep", checked=True), " ",
              t("connectors.preview_match_keep", lang, service=service)),
        Label(Input(type="radio", name="match", value="take"), " ",
              t("connectors.preview_match_take", lang, service=service)),
        Input(type="hidden", name="orders_from", value=orders_from),
        P(t("connectors.preview_start_hint", lang, service=service), cls="settings-hint"),
        Div(
            Button(Span(t("connectors.start_first_sync", lang)), Span(cls="btn-spinner"),
                   type="submit", cls="btn btn--primary", hx_disabled_elt="this"),
            A(t("btn.cancel", lang), href=f"/settings/connectors/{platform}", cls="btn btn--outline"),
            cls="connector-action-area",
        ),
        Div(id=f"first-sync-start-result-{platform}"),
        hx_post=f"/settings/connectors/{platform}/first-sync",
        hx_target=f"#first-sync-start-result-{platform}",
        cls="settings-card first-sync-start",
    )


def first_sync_summary_card(platform: str, summary: dict, lang: str, error: str | None = None) -> FT:
    """What the first sync did and the ways back: Import History undoes the products it
    created with their opening stock; Undo links detaches the products it linked and
    brings back values it took from the store; each imported invoice is voided on its
    own, which keeps its audit trail (posted accounting is never deleted)."""
    if not summary.get("first_sync_at"):
        return Span()
    service = get_connector(platform).display_name
    batch = summary.get("batch")
    links = summary.get("links") or 0
    undone = summary.get("links_undone_at")
    invoices = summary.get("invoices") or []
    return Div(
        P(t("connectors.first_sync_title", lang), cls="settings-section-title"),
        P(t("connectors.first_sync_started", lang, when=_when(summary["first_sync_at"]))),
        P(t("connectors.first_sync_created", lang, n=batch["rows"]), " ",
          A(t("connectors.first_sync_import_history", lang), href="/settings/inventory?tab=import-history"))
        if batch else Span(),
        Div(
            P(t("connectors.first_sync_links_undone" if undone else "connectors.first_sync_links",
                lang, n=links, service=service)),
            *([] if undone else [Button(
                t("connectors.first_sync_undo_links", lang), cls="btn btn--sm btn--outline",
                hx_post=f"/settings/connectors/{platform}/first-sync/undo-links",
                hx_target=f"#first-sync-summary-{platform}", hx_swap="outerHTML",
                hx_confirm=t("connectors.first_sync_undo_links_confirm", lang, n=links, service=service),
            )]),
        ) if links else Span(),
        Div(
            P(t("connectors.first_sync_invoices", lang, n=len(invoices))),
            Table(
                Thead(Tr(Th(t("connectors.preview_order", lang)), Th(t("connectors.status", lang)),
                         Th(t("connectors.preview_total", lang), cls="cell--number"))),
                Tbody(*[Tr(Td(A(_cell(i.get("number")), href=f"/docs/{i['entity_id']}")),
                           Td(_cell(i.get("status"))),
                           Td(_cell(fmt_price(i.get("total"))), cls="cell--number"), cls="data-row")
                        for i in invoices]),
                cls="data-table",
            ),
        ) if invoices else Span(),
        Span(error, cls="flash flash--warning") if error else Span(),
        id=f"first-sync-summary-{platform}",
        cls="settings-card",
    )


def setup_first_sync_routes(app) -> None:
    from ui.routes import settings_connectors as sc
    from ui.routes.settings import _check_permission, _token

    async def _guard(request: Request, platform: str):
        token = _token(request)
        if not token:
            return None, Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return None, r
        if (err := sc._validate_platform(platform)):
            return None, err
        return token, None

    @app.get("/settings/connectors/{platform}/first-sync")
    async def first_sync_preview(request: Request, platform: str):
        """The first sync's preview: counts and tables for the chosen directions,
        the order date, how matching products link, and Start."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if sc._validate_platform(platform) is not None:
            return RedirectResponse("/settings/cloud?tab=website", status_code=302)
        from ui.api_client import APIError, get_first_sync_preview
        from ui.components.shell import base_shell, page_header
        from ui.components.table import breadcrumbs

        lang = get_lang(request)
        company_id = sc._request_company_id(request)
        config = await sc._get_connector_config(company_id, platform)
        if config is None or config.first_sync_at is not None:
            return RedirectResponse(f"/settings/connectors/{platform}", status_code=302)
        show = request.query_params.get("show", "")
        orders_from = request.query_params.get("orders_from", "")
        try:
            preview = await get_first_sync_preview(token, platform, orders_from or None)
        except APIError as exc:
            preview, start_date = None, orders_from
            body = Div(Span(refusal_text(exc.detail) or str(exc.detail), cls="flash flash--warning"),
                       id=f"first-sync-preview-{platform}")
        else:
            start_date = orders_from or str(preview["orders_from"])[:10]
            body = _preview_body(platform, preview, show, start_date, lang)
        if request.query_params.get("fragment"):
            return body
        service = get_connector(platform).display_name
        tab = get_connector(platform).category.value
        return await base_shell(
            breadcrumbs([(t("settings.tab_connectors", lang), f"/settings/cloud?tab={tab}"),
                         (service, f"/settings/connectors/{platform}"),
                         (t("connectors.review_first_sync", lang), None)]),
            page_header(t("connectors.preview_title", lang, service=service),
                        A(t("connectors.back_to_connectors", lang), href=f"/settings/connectors/{platform}",
                          cls="btn btn--secondary btn--sm")),
            P(t("connectors.preview_intro", lang, service=service), cls="settings-hint"),
            Div(sc._entity_direction_rows(platform, config.entity_directions, lang, detail=True),
                cls="settings-card"),
            Div(body, cls="settings-card"),
            _start_form(platform, start_date, lang) if preview is not None else Span(),
            title=f"{t('connectors.review_first_sync', lang)} - {service}",
            nav_active="settings",
            request=request,
        )

    @app.post("/settings/connectors/{platform}/first-sync")
    async def first_sync_start(request: Request, platform: str):
        """Start the first sync with the choices on the preview, then go to the
        connector's page, where it shows its progress."""
        token, refused = await _guard(request, platform)
        if refused is not None:
            return refused
        from ui.api_client import APIError, start_first_sync

        lang = get_lang(request)
        form = await request.form()
        config = await sc._get_connector_config(sc._request_company_id(request), platform)
        try:
            await start_first_sync(token, platform, dict(config.entity_directions if config else {}),
                                   str(form.get("match") or "keep"), str(form.get("orders_from") or "") or None)
        except APIError as exc:
            return Span(refusal_text(exc.detail) or str(exc.detail), cls="flash flash--warning")
        return Response(status_code=204, headers={"HX-Redirect": f"/settings/connectors/{platform}"})

    @app.post("/settings/connectors/{platform}/first-sync/undo-links")
    async def first_sync_undo_links(request: Request, platform: str):
        """Undo the links the first sync made, then show the summary again."""
        token, refused = await _guard(request, platform)
        if refused is not None:
            return refused
        from ui.api_client import APIError, get_first_sync_summary, undo_first_sync_links

        lang = get_lang(request)
        error = None
        try:
            await undo_first_sync_links(token, platform)
        except APIError as exc:
            error = refusal_text(exc.detail) or str(exc.detail)
        return first_sync_summary_card(platform, await get_first_sync_summary(token, platform), lang, error)
