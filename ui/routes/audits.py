# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Inventory audit UI: a scan-first count screen for a location-bound audit.

Scan a barcode to confirm a batch is present (it highlights and jumps to the top); optionally edit a
line's counted quantity; then Adjust stock (manager/owner) to apply the changes - reversibly.
See context/2026-0614-inventory-audit-scanner-plan.md.
"""
from __future__ import annotations

import json

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.components.table import EMPTY
from ui.config import get_token as _token

_STATUS_BADGE = {
    "unaudited": ("Unaudited", "badge--unaudited"),
    "audited": ("Audited", "badge--audited"),
    "stock_adjusted": ("Stock adjusted", "badge--stock-adjusted"),
}


def _status_badge(status: str) -> FT:
    label, cls = _STATUS_BADGE.get(status, (status.replace("_", " ").title(), "badge--unaudited"))
    return Span(label, cls=f"badge {cls}")


def _toast_error(msg: str) -> HTMLResponse:
    """Standard lower-right error toast; no swap."""
    return HTMLResponse("", status_code=200, headers={
        "HX-Reswap": "none",
        "HX-Trigger": json.dumps({"celerpToast": {"message": str(msg), "type": "error"}}),
    })


def _line_row(audit_id: str, li: dict, status: str) -> FT:
    item_id = li.get("item_id", "")
    audited = li.get("audited_at") is not None
    adjusted = bool(li.get("adjusted"))
    on_hand = float(li.get("quantity") or 0)
    counted = li.get("counted_qty")
    row_cls = "data-row"
    if adjusted:
        row_cls += " data-row--adjusted"
    elif audited:
        row_cls += " data-row--audited"
    # Counted is editable until the audit is adjusted; empty = no change (current on-hand kept).
    if status == "stock_adjusted":
        counted_cell = Td(f"{counted:g}" if counted is not None else EMPTY, cls="cell--number")
    else:
        counted_cell = Td(Input(
            type="number", step="any", name="counted_qty",
            value=("" if counted is None else f"{float(counted):g}"),
            placeholder=f"{on_hand:g}",
            hx_post=f"/audits/{audit_id}/line/{item_id}", hx_trigger="change", hx_swap="none",
            cls="cell-input cell-input--number audit-count-input"), cls="cell--number")
    return Tr(
        Td(li.get("sku") or EMPTY),
        Td(li.get("name") or EMPTY),
        Td(f"{on_hand:g}", cls="cell--number"),
        counted_cell,
        cls=row_cls,
    )


def _audit_body(audit: dict, location_name: str) -> FT:
    """The swappable audit body: status, progress, scan bar, actions and the line table."""
    aid = audit.get("id", "")
    status = audit.get("status", "unaudited")
    lines = audit.get("line_items") or []
    audited_n = sum(1 for l in lines if l.get("audited_at") is not None)

    scan_bar = ""
    if status != "stock_adjusted":
        scan_bar = Div(
            Span("📷", cls="scan-bar-icon"),
            Input(type="text", id="audit-scan-input", name="barcode", autocomplete="off",
                  placeholder="Scan barcode or type a SKU and press Enter",
                  hx_post=f"/audits/{aid}/scan", hx_target="#audit-body", hx_swap="outerHTML",
                  hx_trigger="keyup[key=='Enter']",
                  cls="scan-bar-input"),
            cls="scan-bar",
        )

    actions = []
    if status == "unaudited":
        actions.append(_act("Done auditing", aid, "done", primary=True))
    elif status == "audited":
        actions.append(_act("Adjust stock", aid, "adjust", primary=True,
                            confirm="Apply the counted quantities to stock? This posts a journal entry."))
        actions.append(_act("Reopen", aid, "reopen", primary=False))
    elif status == "stock_adjusted":
        actions.append(_act("Undo stock adjustment", aid, "undo-adjust", primary=False,
                            confirm="Reverse this audit's stock adjustment?"))

    rows = [_line_row(aid, li, status) for li in lines]
    table = Table(
        Thead(Tr(Th("SKU"), Th("Item"), Th("On hand", cls="cell--number"), Th("Counted", cls="cell--number"))),
        Tbody(*rows) if rows else Tbody(Tr(Td("No items at this location.", colspan="4", cls="empty-row"))),
        cls="data-table", id="audit-table",
    )

    return Div(
        Div(
            _status_badge(status),
            Span(f"Location: {location_name}", cls="hint"),
            Span(f"Audited {audited_n} / {len(lines)}", cls="hint", id="audit-progress"),
            cls="audit-meta",
        ),
        scan_bar,
        Div(*actions, cls="audit-actions") if actions else "",
        table,
        id="audit-body",
        # Keep the scanner field focused after every re-render so scans can be fired back-to-back.
        **{"hx_on::after-settle": "var i=this.querySelector('#audit-scan-input'); if(i) i.focus()"},
    )


def _act(label: str, aid: str, action: str, *, primary: bool, confirm: str | None = None) -> FT:
    attrs = {"hx_post": f"/audits/{aid}/{action}", "hx_target": "#audit-body", "hx_swap": "outerHTML",
             "hx_disabled_elt": "this"}
    if confirm:
        attrs["hx_confirm"] = confirm
    return Button(label, type="button", cls=f"btn btn--sm btn--{'primary' if primary else 'secondary'}", **attrs)


def setup_routes(app):

    async def _body_response(token: str, entity_id: str):
        audit = await api.get_audit(token, entity_id)
        loc_name = ""
        try:
            locs = {l.get("id"): l.get("name") for l in (await api.get_locations(token)).get("items", [])}
            loc_name = locs.get(audit.get("location_id"), "") or ""
        except APIError:
            pass
        return _audit_body(audit, loc_name)

    @app.get("/audits")
    async def audits_list(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            audits = (await api.list_lists(token, {"list_type": "audit"})).get("items", [])
        except APIError:
            audits = []
        rows = [
            Tr(
                Td(A(a.get("ref_id") or a.get("id"), href=f"/audits/{a.get('id')}", cls="table-link")),
                Td(_status_badge(a.get("status", "unaudited"))),
                Td((a.get("created_at") or "")[:10]),
                cls="data-row",
            )
            for a in audits
        ]
        table = Table(
            Thead(Tr(Th("Audit"), Th("Status"), Th("Created"))),
            Tbody(*rows) if rows else Tbody(Tr(Td("No audits yet.", colspan="3", cls="empty-row"))),
            cls="data-table",
        )
        return base_shell(
            page_header("Inventory Audits",
                        A("New audit", href="/audits/new", cls="btn btn--sm btn--primary")),
            table, title="Inventory Audits - Celerp", nav_active="inventory", request=request,
        )

    @app.get("/audits/new")
    async def audit_new_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            locations = (await api.get_locations(token)).get("items", [])
        except APIError:
            locations = []
        if not locations:
            return base_shell(
                page_header("New Audit"),
                P("Create a location first (Settings > Inventory > Locations) to audit it.", cls="hint"),
                title="New Audit - Celerp", nav_active="inventory", request=request,
            )
        form = Form(
            Div(Label("Location", For="location_id", cls="form-label"),
                Select(*[Option(l.get("name"), value=l.get("id")) for l in locations],
                       name="location_id", id="location_id", cls="form-input form-input--sm"),
                P("The audit is pre-populated with the stocked items at this location.", cls="hint"),
                cls="form-group"),
            Button("Start audit", type="submit", cls="btn btn--primary"),
            method="post", action="/audits/new", cls="form-card",
        )
        return base_shell(page_header("New Audit"), form,
                          title="New Audit - Celerp", nav_active="inventory", request=request)

    @app.post("/audits/new")
    async def audit_create(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        location_id = str(form.get("location_id", "")).strip()
        if not location_id:
            return RedirectResponse("/audits/new", status_code=303)
        try:
            res = await api.create_audit(token, location_id)
        except APIError:
            return RedirectResponse("/audits/new", status_code=303)
        return RedirectResponse(f"/audits/{res['id']}", status_code=303)

    @app.get("/audits/{entity_id}")
    async def audit_detail(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            audit = await api.get_audit(token, entity_id)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return base_shell(page_header("Audit"), P(e.detail, cls="cell-error"),
                              title="Audit - Celerp", nav_active="inventory", request=request)
        ref = audit.get("ref_id") or entity_id
        body = await _body_response(token, entity_id)
        return base_shell(
            page_header(f"Audit {ref}", A("All audits", href="/audits", cls="btn btn--sm btn--secondary")),
            P("Scan items to confirm they are present, edit any counts, then adjust stock.", cls="hint"),
            body,
            title=f"Audit {ref} - Celerp", nav_active="inventory", request=request,
        )

    @app.post("/audits/{entity_id}/scan")
    async def audit_scan(request: Request, entity_id: str):
        token = _token(request)
        if not token:
            return _toast_error("Session expired.")
        form = await request.form()
        barcode = str(form.get("barcode", "")).strip()
        if not barcode:
            return await _body_response(token, entity_id)
        try:
            await api.scan_audit(token, entity_id, barcode)
        except APIError as e:
            return _toast_error(e.detail)
        return await _body_response(token, entity_id)

    @app.post("/audits/{entity_id}/line/{item_id}")
    async def audit_set_count(request: Request, entity_id: str, item_id: str):
        token = _token(request)
        if not token:
            return _toast_error("Session expired.")
        form = await request.form()
        raw = str(form.get("counted_qty", "")).strip()
        try:
            cq = float(raw) if raw else None
        except ValueError:
            cq = None
        try:
            await api.set_audit_count(token, entity_id, item_id, cq)
        except APIError as e:
            return _toast_error(e.detail)
        return HTMLResponse("", status_code=204)

    @app.post("/audits/{entity_id}/{action}")
    async def audit_action(request: Request, entity_id: str, action: str):
        token = _token(request)
        if not token:
            return _toast_error("Session expired.")
        if action not in ("done", "reopen", "adjust", "undo-adjust"):
            return _toast_error("Unknown action.")
        try:
            await api.audit_action(token, entity_id, action)
        except APIError as e:
            return _toast_error(e.detail)
        return await _body_response(token, entity_id)
