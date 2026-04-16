# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, flash
from ui.components.table import EMPTY, breadcrumbs, status_cards, empty_state_cta, format_value, add_new_option
from ui.config import get_token as _token
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)




def _badge(status: str) -> FT:
    key = (status or "").lower().replace("_", "-")
    label = (status or "").replace("_", " ").title()
    return Span(label or EMPTY, cls=f"badge badge--{key}")


def _mfg_status_cards(orders: list[dict], active_status: str) -> FT:
    _CARD_DEFS = [
        ("planned", "Planned", "blue"),
        ("in_progress", "In Progress", "yellow"),
        ("completed", "Completed", "green"),
        ("cancelled", "Cancelled", "gray"),
    ]
    counts: dict[str, int] = {s: 0 for s, _, _ in _CARD_DEFS}
    for o in orders:
        s = str(o.get("status") or "").lower()
        if s in counts:
            counts[s] += 1
        # "draft" / "pending" maps to planned visually
        elif s in ("draft", "pending"):
            counts["planned"] += 1
    cards = [
        {"label": label, "count": counts[s], "status": s, "color": color}
        for s, label, color in _CARD_DEFS
    ]
    return status_cards(cards, "/manufacturing", active_status or None)


def _order_row(order: dict) -> FT:
    oid = order.get("entity_id", "")
    short_id = oid.split(":")[-1][:8] if oid else EMPTY
    inputs = order.get("inputs", [])
    return Tr(
        Td(A(f"#{short_id}", href=f"/manufacturing/{oid}", cls="link")),
        Td(format_value(order.get("order_type", order.get("description", "")))),
        Td(_badge(order.get("status", "draft"))),
        Td(format_value((order.get("created_at") or "")[:10])),
        Td(str(len(inputs)), cls="cell--number"),
    )


def _order_table(orders: list[dict]) -> FT:
    if not orders:
        return Div(
            empty_state_cta("No production orders.", "Create Order", "/manufacturing/new"),
            id="mfg-table",
        )
    return Table(
        Thead(Tr(
            Th(t("th.order")), Th(t("th.doc_type")), Th(t("th.status")), Th(t("msg.created")), Th(t("th.inputs")),
        )),
        Tbody(*[_order_row(o) for o in orders]),
        cls="data-table",
        id="mfg-table",
    )


def _bom_section(order: dict) -> FT:
    inputs = order.get("inputs", [])
    outputs = order.get("expected_outputs", [])
    steps_done = set(order.get("steps_completed", []))
    status = order.get("status", "draft")
    oid = order.get("entity_id", "")

    # T8: Inputs with consume buttons
    input_rows = []
    for inp in inputs:
        iid = inp.get("item_id", "")
        consumed_qty = float(inp.get("consumed_qty", 0) or 0)
        required_qty = float(inp.get("quantity", 0) or 0)
        consumed = f"consume:{iid}" in steps_done or consumed_qty >= required_qty

        consume_btn = ""
        if status == "in_progress" and not consumed:
            consume_btn = Details(
                Summary(t("mfg.consume"), cls="btn btn--primary btn--xs"),
                Form(
                    Div(
                        Label(t("th.qty"), cls="form-label"),
                        Input(type="number", name="quantity", value=str(required_qty - consumed_qty),
                              step="any", min="0", cls="form-input form-input--sm"),
                        cls="form-group",
                    ),
                    Input(type="hidden", name="item_id", value=iid),
                    Button(t("btn.confirm"), type="submit", cls="btn btn--primary btn--xs"),
                    hx_post=f"/manufacturing/{oid}/consume",
                    hx_target="#mfg-detail",
                    hx_swap="outerHTML",
                    cls="form-card",
                ),
            )

        input_rows.append(Tr(
            Td(format_value(iid)),
            Td(format_value(required_qty), cls="cell--number"),
            Td(str(consumed_qty), cls="cell--number"),
            Td("✓ Consumed" if consumed else "Pending", cls="cell--number"),
            Td(consume_btn),
        ))

    output_rows = [
        Tr(
            Td(format_value(o.get("sku"))),
            Td(format_value(o.get("name"))),
            Td(format_value(o.get("quantity")), cls="cell--number"),
        )
        for o in outputs
    ]

    # T8: Steps checklist
    steps = order.get("steps", [])
    step_rows = []
    for step in steps:
        sid = step.get("step_id", "")
        step_status = step.get("status", "pending")
        step_done = step_status in ("completed", "done")

        complete_btn = ""
        if status == "in_progress" and not step_done:
            complete_btn = Details(
                Summary(t("mfg.complete_step"), cls="btn btn--primary btn--xs"),
                Form(
                    Div(
                        Label(t("label.notes_optional"), cls="form-label"),
                        Textarea("", name="notes", rows="2", cls="form-input form-input--sm"),
                        cls="form-group",
                    ),
                    Input(type="hidden", name="step_id", value=sid),
                    Button(t("btn.confirm"), type="submit", cls="btn btn--primary btn--xs"),
                    hx_post=f"/manufacturing/{oid}/step",
                    hx_target="#mfg-detail",
                    hx_swap="outerHTML",
                    cls="form-card",
                ),
            )

        step_rows.append(Tr(
            Td("✓" if step_done else "○", cls="cell--number"),
            Td(str(step.get("name", sid))),
            Td(_badge(step_status)),
            Td(complete_btn),
        ))

    steps_section = ""
    if steps:
        steps_section = Div(
            H3(t("page.steps")),
            Table(
                Thead(Tr(Th(""), Th(t("th.step")), Th(t("th.status")), Th(""))),
                Tbody(*step_rows),
                cls="data-table data-table--compact",
            ),
            cls="steps-panel",
        )

    return Div(
        steps_section,
        Div(
            H3(t("page.inputs_bom")),
            Table(
                Thead(Tr(Th(t("label.item_id")), Th(t("th.required")), Th(t("th.consumed")), Th(t("th.status")), Th(""))),
                Tbody(*input_rows) if input_rows else Tbody(Tr(Td(t("mfg.no_inputs_defined"), colspan="5"))),
                cls="data-table data-table--compact",
            ),
            cls="bom-panel",
        ),
        Div(
            H3(t("page.expected_outputs")),
            Table(
                Thead(Tr(Th("SKU"), Th(t("th.name")), Th(t("th.quantity")))),
                Tbody(*output_rows) if output_rows else Tbody(Tr(Td(t("mfg.no_outputs_defined"), colspan="3"))),
                cls="data-table data-table--compact",
            ),
            cls="bom-panel",
        ),
        cls="bom-grid",
    )


def _action_buttons(order: dict, order_id: str) -> FT:
    status = order.get("status", "draft")
    btns = []
    if status in ("draft", "pending"):
        btns.append(
            Form(
                Button(t("btn.start_order"), cls="btn btn--primary", type="submit"),
                method="post", action=f"/manufacturing/{order_id}/start",
                hx_post=f"/manufacturing/{order_id}/start",
                hx_target="#mfg-detail",
                hx_swap="outerHTML",
            )
        )
    if status == "in_progress":
        btns.append(
            Form(
                Button(t("btn.complete_order"), cls="btn btn--primary", type="submit"),
                method="post", action=f"/manufacturing/{order_id}/complete",
                hx_post=f"/manufacturing/{order_id}/complete",
                hx_target="#mfg-detail",
                hx_swap="outerHTML",
            )
        )
    if status not in ("completed", "cancelled"):
        btns.append(
            Form(
                Button(t("btn.cancel_order"), cls="btn btn--secondary", type="submit"),
                method="post", action=f"/manufacturing/{order_id}/cancel",
                hx_post=f"/manufacturing/{order_id}/cancel",
                hx_target="#mfg-detail",
                hx_swap="outerHTML",
            )
        )
    return Div(*btns, cls="action-bar") if btns else Div()


def _detail_panel(order: dict) -> FT:
    oid = order.get("entity_id", "")
    short_id = oid.split(":")[-1][:8] if oid else EMPTY
    return Div(
        Div(
            Div(
                Span(t("th.order"), cls="detail-label"),
                Span(f"#{short_id}", cls="detail-value"),
            ),
            Div(
                Span(t("th.doc_type"), cls="detail-label"),
                Span(format_value(order.get("order_type", order.get("description", ""))), cls="detail-value"),
            ),
            Div(
                Span(t("th.status"), cls="detail-label"),
                _badge(order.get("status", "draft")),
            ),
            Div(
                Span(t("th.description"), cls="detail-label"),
                Span(format_value(order.get("description")), cls="detail-value"),
            ),
            Div(
                Span(t("th.due_date"), cls="detail-label"),
                Span(format_value(order.get("due_date")), cls="detail-value"),
            ),
            Div(
                Span(t("mfg.est_cost"), cls="detail-label"),
                Span(format_value(order.get("estimated_cost")), cls="detail-value"),
            ),
            cls="detail-fields",
        ),
        _bom_section(order),
        _action_buttons(order, oid),
        id="mfg-detail",
    )


def setup_routes(app):

    @app.get("/manufacturing/boms/new")
    async def new_bom_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        return base_shell(
            page_header(t("page.new_bom", lang), A(t("btn.cancel", lang), href="/manufacturing/boms", cls="btn btn--secondary")),
            _new_bom_form(),
            title="New BOM - Celerp",
            nav_active="manufacturing",
            lang=lang,
            request=request,
        )

    @app.post("/manufacturing/boms/new")
    async def create_bom_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        name = str(form.get("name", "")).strip()
        output_item_id = str(form.get("output_item_id", "")).strip() or None
        output_qty_str = str(form.get("output_qty", "1"))
        try:
            output_qty = float(output_qty_str)
        except ValueError:
            output_qty = 1.0
        data = {"name": name, "output_item_id": output_item_id, "output_qty": output_qty, "components": []}
        try:
            result = await api.create_bom(token, data)
            bom_id = result["bom_id"]
            return RedirectResponse(f"/manufacturing/boms/{bom_id}", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return base_shell(
                page_header("New BOM", A(t("btn.cancel"), href="/manufacturing/boms", cls="btn btn--secondary")),
                flash(e.detail),
                _new_bom_form({"name": name, "output_item_id": output_item_id}),
                title="New BOM - Celerp",
                nav_active="manufacturing",
                request=request,
            )

    @app.get("/manufacturing/boms")
    async def boms_list(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            boms = (await api.list_boms(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            boms = []
        lang = get_lang(request)
        return base_shell(
            page_header(t("page.new_bom", lang).replace("New ", "") + "s", A(t("page.new_bom", lang), href="/manufacturing/boms/new", cls="btn btn--primary")),
            _bom_list_table(boms),
            title="BOMs - Celerp",
            nav_active="manufacturing",
            lang=lang,
            request=request,
        )

    @app.get("/manufacturing/boms/{bom_id}")
    async def bom_detail_page(request: Request, bom_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            bom = await api.get_bom(token, bom_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if isinstance(e, APIError) and e.status == 404:
                return RedirectResponse("/manufacturing/boms", status_code=302)
            bom = {}
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Manufacturing", "/manufacturing"), ("BOMs", "/manufacturing/boms"), (bom.get('name') or EMPTY, None)]),
            page_header(
                f"BOM: {bom.get('name') or EMPTY}",
                A(t("btn._boms"), href="/manufacturing/boms", cls="btn btn--secondary"),
                A(t("btn.delete"), href=f"/manufacturing/boms/{bom_id}/delete", cls="btn btn--danger btn--sm"),
            ),
            _bom_detail_section(bom),
            title=f"BOM {bom.get('name', '')} - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/boms/{bom_id}/save")
    async def save_bom(request: Request, bom_id: str):
        from starlette.responses import Response as _R
        token = _token(request)
        if not token:
            return _R("", status_code=401, headers={"HX-Redirect": "/login"})
        try:
            body = await request.json()
        except Exception as e:
            from starlette.responses import JSONResponse
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)
        try:
            await api.update_bom(token, bom_id, body)
        except APIError as e:
            from starlette.responses import JSONResponse
            return JSONResponse({"error": str(e.detail)}, status_code=400)
        from starlette.responses import JSONResponse
        return JSONResponse({"ok": True})

    @app.get("/manufacturing/boms/{bom_id}/delete")
    async def delete_bom_page(request: Request, bom_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.delete_bom(token, bom_id)
        except APIError as e:
            logger.warning("API error on delete BOM %s: %s", bom_id, e.detail)
        return RedirectResponse("/manufacturing/boms", status_code=302)

    @app.get("/manufacturing")
    async def manufacturing_list(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            orders = (await api.list_mfg_orders(token)).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            orders = []
        return base_shell(
            page_header(
                "Manufacturing",
                A(t("mfg.bills_of_materials"), href="/manufacturing/boms", cls="btn btn--secondary"),
                A(t("doc.import_csv"), href="/manufacturing/import", cls="btn btn--secondary"),
                A(t("btn.new_order"), href="/manufacturing/new", cls="btn btn--primary"),
            ),
            _mfg_status_cards(orders, request.query_params.get("status", "")),
            _order_table(orders),
            title="Manufacturing - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/new")
    async def new_mfg_order(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            items = (await api.list_items(token, {"limit": 500})).get("items", [])
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            items = []
        return base_shell(
            page_header("New Manufacturing Order", A(t("btn.cancel"), href="/manufacturing", cls="btn btn--secondary")),
            _new_order_form(items),
            title="New Manufacturing Order - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/new")
    async def create_mfg_order(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        # Parse inputs: item_id_0, qty_0, item_id_1, qty_1, ...
        inputs = []
        i = 0
        while f"item_id_{i}" in form:
            iid = str(form.get(f"item_id_{i}", "")).strip()
            qty_str = str(form.get(f"qty_{i}", "0"))
            try:
                qty = float(qty_str)
            except ValueError:
                qty = 0.0
            if iid and qty > 0:
                inputs.append({"item_id": iid, "quantity": qty})
            i += 1
        data = {
            "description": str(form.get("description", "")).strip(),
            "order_type": str(form.get("order_type", "assembly")),
            "inputs": inputs,
            "notes": str(form.get("notes", "")).strip() or None,
        }
        due = str(form.get("due_date", "")).strip()
        if due:
            data["due_date"] = due
        try:
            await api.create_mfg_order(token, data)
            return RedirectResponse("/manufacturing", status_code=302)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            try:
                items = (await api.list_items(token, {"limit": 500})).get("items", [])
            except APIError:
                items = []
            return base_shell(
                page_header("New Manufacturing Order", A(t("btn.cancel"), href="/manufacturing", cls="btn btn--secondary")),
                flash(e.detail),
                _new_order_form(items, data),
                title="New Manufacturing Order - Celerp",
                nav_active="manufacturing",
                request=request,
            )

    @app.get("/manufacturing/{order_id:path}")
    async def mfg_order_detail(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            order = await api.get_mfg_order(token, order_id)
        except (APIError, Exception) as e:
            if isinstance(e, APIError) and e.status == 401:
                return RedirectResponse("/login", status_code=302)
            if isinstance(e, APIError) and e.status == 404:
                return RedirectResponse("/manufacturing", status_code=302)
            order = {}
        oid = order.get("entity_id", order_id)
        short_id = oid.split(":")[-1][:8] if oid else order_id
        return base_shell(
            breadcrumbs([("Dashboard", "/dashboard"), ("Manufacturing", "/manufacturing"), (f"Order #{short_id}", None)]),
            page_header(
                f"Manufacturing Order",
                A(t("btn.back_to_settings"), href="/manufacturing", cls="btn btn--secondary"),
            ),
            _detail_panel(order),
            title="Manufacturing Order - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/{order_id:path}/start")
    async def start_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.start_mfg_order(token, order_id)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    @app.post("/manufacturing/{order_id:path}/complete")
    async def complete_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await api.complete_mfg_order(token, order_id)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    @app.post("/manufacturing/{order_id:path}/cancel")
    async def cancel_mfg_order(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        reason = str(form.get("reason", "")).strip() or None
        try:
            await api.cancel_mfg_order(token, order_id, reason)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    # T8: Complete step
    @app.post("/manufacturing/{order_id:path}/step")
    async def complete_step_route(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        step_id = str(form.get("step_id", "")).strip()
        notes = str(form.get("notes", "")).strip() or None
        try:
            await api.complete_mfg_step(token, order_id, step_id, notes)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")

    # T8: Consume input
    @app.post("/manufacturing/{order_id:path}/consume")
    async def consume_input_route(request: Request, order_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        item_id = str(form.get("item_id", "")).strip()
        try:
            quantity = float(str(form.get("quantity", "0")))
        except ValueError:
            quantity = 0.0
        try:
            await api.consume_mfg_input(token, order_id, item_id, quantity)
            order = await api.get_mfg_order(token, order_id)
            return _detail_panel(order)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            return Div(flash(e.detail), id="mfg-detail")


def _new_order_form(items: list[dict], prefill: dict | None = None) -> FT:
    p = prefill or {}
    _item_opt, _item_js = add_new_option("+ Add new item", "/inventory")
    item_options = [Option("-- select item --", value="")] + [
        Option(f"{it.get('sku', '')} - {it.get('name', '')}", value=it.get("entity_id", ""))
        for it in items
    ] + [_item_opt]
    if not items:
        item_select = Div(
            P(t("mfg.no_inventory_items_found"), A(t("mfg.add_items"), href="/inventory")),
        )
    else:
        item_select = Select(*item_options, name="item_id_0", cls="input-row-item", onchange=_item_js)
    return Form(
        Div(
            Label(t("label.description"), For="description"),
            Input(id="description", name="description", value=p.get("description", ""), required=True, placeholder="e.g. Assemble Widget A"),
            cls="form-group",
        ),
        Div(
            Label(t("label.order_type"), For="order_type"),
            Select(
                Option(t("mfg.assembly"), value="assembly", selected=p.get("order_type", "assembly") == "assembly"),
                Option(t("mfg.disassembly"), value="disassembly", selected=p.get("order_type") == "disassembly"),
                Option(t("mfg.processing"), value="processing", selected=p.get("order_type") == "processing"),
                id="order_type", name="order_type",
            ),
            cls="form-group",
        ),
        Div(
            Label(t("th.due_date"), For="due_date"),
            Input(type="date", id="due_date", name="due_date", value=p.get("due_date", "")),
            cls="form-group",
        ),
        Div(
            H3(t("page.inputs_bom")),
            Div(
                Div(
                    item_select,
                    Input(type="number", name="qty_0", value="1", min="0.001", step="any", placeholder="Qty", cls="input-row-qty"),
                    cls="input-row",
                ),
                id="inputs-container",
            ),
            Button(t("btn._add_input"), type="button", cls="btn btn--secondary btn--xs",
                   onclick="addInput()"),
            cls="form-group",
        ),
        Div(
            Label(t("th.notes"), For="notes"),
            Textarea(p.get("notes", ""), id="notes", name="notes", rows="3"),
            cls="form-group",
        ),
        Button(t("btn.create_order"), cls="btn btn--primary", type="submit"),
        method="post", action="/manufacturing/new",
    )


def _bom_list_table(boms: list[dict]) -> FT:
    if not boms:
        return Div(
            empty_state_cta("No BOMs yet.", "Create Order", "/manufacturing/boms/new"),
            id="bom-table",
        )
    return Table(
        Thead(Tr(Th(t("th.name")), Th(t("th.output_item")), Th(t("label.output_qty")), Th(t("th.components")), Th(""))),
        Tbody(*[
            Tr(
                Td(A(b.get("name", EMPTY), href=f"/manufacturing/boms/{b.get('bom_id', '')}", cls="link")),
                Td(format_value(b.get("output_item_id"))),
                Td(format_value(b.get("output_qty")), cls="cell--number"),
                Td(str(len(b.get("components", []))), cls="cell--number"),
                Td(A(t("btn.delete"), href=f"/manufacturing/boms/{b.get('bom_id', '')}/delete", cls="btn btn--danger btn--xs")),
            )
            for b in boms if not b.get("deleted")
        ]),
        cls="data-table",
        id="bom-table",
    )


def _bom_detail_section(bom: dict) -> FT:
    bom_id = bom.get("bom_id", "")
    components = bom.get("components", [])

    def _comp_row(c: dict, idx: int) -> FT:
        return Tr(
            Td(Input(type="text", value=c.get("sku", ""), data_name="sku",
                     placeholder="SKU", cls="cell-input cell-input--sm")),
            Td(Input(type="text", value=c.get("item_id", "") or "",
                     data_name="item_id", placeholder="item_id", cls="cell-input cell-input--sm")),
            Td(Input(type="number", value=str(c.get("qty", 1)), step="any",
                     data_name="qty", cls="cell-input cell-input--xs")),
            Td(Input(type="text", value=c.get("unit", "pieces"),
                     data_name="unit", cls="cell-input cell-input--xs")),
            Td(Button("✕", type="button", cls="btn btn--danger btn--xs",
                      onclick="this.closest('tr').remove();")),
        )

    existing = [_comp_row(c, i) for i, c in enumerate(components)]

    return Div(
        Div(
            Table(
                Tbody(
                    Tr(Td(t("th.name"), cls="detail-label"),
                       Td(Input(type="text", id="bom-name", value=bom.get("name", ""),
                                cls="form-input"))),
                    Tr(Td(t("mfg.output_item_id"), cls="detail-label"),
                       Td(Input(type="text", id="bom-output-item", value=bom.get("output_item_id", "") or "",
                                cls="form-input"))),
                    Tr(Td(t("label.output_qty"), cls="detail-label"),
                       Td(Input(type="number", id="bom-output-qty", value=str(bom.get("output_qty", 1)),
                                step="any", cls="form-input"))),
                ),
                cls="detail-table",
            ),
            cls="detail-card",
        ),
        H3(t("th.components")),
        Template(
            Tr(
                Td(Input(type="text", data_name="sku", placeholder="SKU", cls="cell-input cell-input--sm")),
                Td(Input(type="text", data_name="item_id", placeholder="item_id", cls="cell-input cell-input--sm")),
                Td(Input(type="number", value="1", step="any", data_name="qty", cls="cell-input cell-input--xs")),
                Td(Input(type="text", value="pieces", data_name="unit", cls="cell-input cell-input--xs")),
                Td(Button("✕", type="button", cls="btn btn--danger btn--xs",
                          onclick="this.closest('tr').remove();")),
            ),
            id="comp-row-tpl",
        ),
        Table(
            Thead(Tr(Th("SKU"), Th(t("label.item_id")), Th(t("th.qty")), Th(t("th.unit")), Th(""))),
            Tbody(
                *(existing if existing else [
                    Tr(Td(t("mfg.no_components_yet"), colspan="5", cls="empty-state-msg"), id="empty-comp-hint"),
                ]),
                id="comp-body",
            ),
            cls="data-table",
        ),
        Div(
            Button(t("btn._add_component"), type="button", cls="btn btn--secondary",
                   onclick="addComponent()"),
            Button(t("btn.save_bom"), type="button", cls="btn btn--primary",
                   onclick="saveBOM()"),
            Span("", id="bom-save-status", cls="save-status"),
            cls="line-actions",
        ),
        Script(f"""
const BOM_ID = {repr(bom_id)};

function addComponent() {{
    const tpl = document.getElementById('comp-row-tpl').content.cloneNode(true);
    const hint = document.getElementById('empty-comp-hint');
    if (hint) hint.remove();
    document.getElementById('comp-body').appendChild(tpl);
}}

async function saveBOM() {{
    const rows = document.querySelectorAll('#comp-body tr');
    const components = [];
    rows.forEach(row => {{
        const sku = row.querySelector('[data-name="sku"]')?.value;
        const item_id = row.querySelector('[data-name="item_id"]')?.value;
        const qty = parseFloat(row.querySelector('[data-name="qty"]')?.value || 0);
        const unit = row.querySelector('[data-name="unit"]')?.value || 'pieces';
        if (sku || item_id) components.push({{sku: sku || '', item_id: item_id || null, qty, unit}});
    }});
    const payload = {{
        name: document.getElementById('bom-name').value,
        output_item_id: document.getElementById('bom-output-item').value || null,
        output_qty: parseFloat(document.getElementById('bom-output-qty').value || 1),
        components,
    }};
    const resp = await fetch('/manufacturing/boms/' + BOM_ID + '/save', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload),
    }});
    const statusEl = document.getElementById('bom-save-status');
    if (resp.ok) {{
        statusEl.textContent = 'Saved \u2713';
        setTimeout(() => {{ statusEl.textContent = ''; }}, 2000);
    }} else {{
        const err = await resp.json().catch(() => ({{}}));
        statusEl.textContent = err.error || 'Save failed';
        statusEl.style.color = 'red';
    }}
}}
"""),
        cls="bom-detail",
    )


def _new_bom_form(prefill: dict | None = None) -> FT:
    p = prefill or {}
    return Form(
        Div(Label(t("label.bom_name"), cls="form-label"),
            Input(type="text", name="name", value=p.get("name", ""), required=True,
                  placeholder="e.g. Ring Assembly v1", cls="form-input"),
            cls="form-group"),
        Div(Label(t("mfg.output_item_id"), cls="form-label"),
            Input(type="text", name="output_item_id", value=p.get("output_item_id", "") or "",
                  placeholder="item:...", cls="form-input"),
            cls="form-group"),
        Div(Label(t("label.output_quantity"), cls="form-label"),
            Input(type="number", name="output_qty", value="1", step="any", cls="form-input"),
            cls="form-group"),
        Button(t("btn.create_bom"), type="submit", cls="btn btn--primary"),
        method="post", action="/manufacturing/boms/new",
        cls="form-card",
    )
