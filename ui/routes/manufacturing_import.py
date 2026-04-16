# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Manufacturing CSV import (BOMs + Mfg Orders).

Design: simple, practical CSV formats that can be filled by humans.
"""

from __future__ import annotations

import csv
import io
import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header
from ui.config import get_token as _token
from ui.routes.csv_import import (
    CsvImportSpec,
    _resolve_csv_text,
    _rows_to_csv,
    _stash_csv,
    apply_column_mapping,
    apply_fixes_to_rows,
    column_mapping_form,
    error_report_response,
    import_result_panel,
    read_csv_upload,
    upload_form,
    validate_cell,
    validate_column_mapping,
    validation_result,
)
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)


_BOM_SPEC = CsvImportSpec(
    cols=["bom_name", "output_sku", "output_qty", "component_sku", "component_qty", "unit"],
    required={"bom_name", "output_sku", "component_sku", "component_qty"},
    type_map={"output_qty": float, "component_qty": float},
)

_ORDER_SPEC = CsvImportSpec(
    cols=["description", "order_type", "bom_name", "location_id", "due_date", "notes"],
    required={"description"},
    type_map={},
)


def setup_routes(app):

    @app.get("/manufacturing/import")
    async def mfg_import_home(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header("Import Manufacturing", A(t("btn.back_to_settings"), href="/manufacturing", cls="btn btn--secondary")),
            Div(
                H3(t("page.bills_of_materials_boms"), cls="section-title"),
                A(t("mfg.import_boms"), href="/manufacturing/import/boms", cls="btn btn--secondary"),
                H3(t("page.manufacturing_orders"), cls="section-title"),
                A(t("mfg.import_orders"), href="/manufacturing/import/orders", cls="btn btn--secondary"),
                cls="section",
            ),
            title="Import Manufacturing - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    # ── BOMs ─────────────────────────────────────────────────────────────

    @app.get("/manufacturing/import/boms")
    async def mfg_import_boms_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header(
                "Import BOMs",
                A(t("btn.back_to_settings"), href="/manufacturing", cls="btn btn--secondary"),
                A(t("btn.download_template"), href="/manufacturing/import/boms/template", cls="btn btn--secondary"),
            ),
            upload_form(
                cols=_BOM_SPEC.cols,
                template_href="/manufacturing/import/boms/template",
                preview_action="/manufacturing/import/boms/preview",
                has_mapping=True,
            ),
            title="Import BOMs - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/import/boms/template")
    async def mfg_import_boms_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=_BOM_SPEC.cols)
        w.writeheader()
        w.writerow({"bom_name": "Ring Assembly", "output_sku": "RING-001", "output_qty": "1", "component_sku": "STONE-001", "component_qty": "1", "unit": "pieces"})
        from starlette.responses import Response
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=boms_template.csv"},
        )

    @app.post("/manufacturing/import/boms/preview")
    async def mfg_import_boms_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import BOMs"),
                upload_form(
                    cols=_BOM_SPEC.cols,
                    template_href="/manufacturing/import/boms/template",
                    preview_action="/manufacturing/import/boms/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import BOMs - Celerp",
                nav_active="manufacturing",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import BOMs"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_BOM_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/manufacturing/import/boms/mapped",
                back_href="/manufacturing/import/boms",
                required_targets=_BOM_SPEC.required,
            ),
            title="Import BOMs - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/import/boms/mapped")
    async def mfg_import_boms_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import BOMs"),
                upload_form(
                    cols=_BOM_SPEC.cols,
                    template_href="/manufacturing/import/boms/template",
                    preview_action="/manufacturing/import/boms/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import BOMs - Celerp",
                nav_active="manufacturing",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_BOM_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import BOMs"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_BOM_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/manufacturing/import/boms/mapped",
                    back_href="/manufacturing/import/boms",
                    required_targets=_BOM_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import BOMs - Celerp",
                nav_active="manufacturing",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _BOM_SPEC.cols)

        return base_shell(
            page_header("Import BOMs"),
            validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v: validate_cell(_BOM_SPEC, c, v),
                confirm_action="/manufacturing/import/boms/confirm",
                error_report_action="/manufacturing/import/boms/errors",
                back_href="/manufacturing/import/boms",
                revalidate_action="/manufacturing/import/boms/revalidate",
                has_mapping=True,
            ),
            title="Import BOMs - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/import/boms/revalidate")
    async def mfg_import_boms_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return upload_form(
                cols=_BOM_SPEC.cols,
                template_href="/manufacturing/import/boms/template",
                preview_action="/manufacturing/import/boms/preview",
                has_mapping=True,
                error="CSV data expired. Please re-upload.",
            )
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _BOM_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v: validate_cell(_BOM_SPEC, c, v),
            confirm_action="/manufacturing/import/boms/confirm",
            error_report_action="/manufacturing/import/boms/errors",
            back_href="/manufacturing/import/boms",
            revalidate_action="/manufacturing/import/boms/revalidate",
            has_mapping=True,
        )

    @app.post("/manufacturing/import/boms/errors")
    async def mfg_import_boms_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        cols = list(rows[0].keys()) if rows else _BOM_SPEC.cols
        return error_report_response(rows, cols, lambda c, v: validate_cell(_BOM_SPEC, c, v), "boms_errors.csv")

    @app.post("/manufacturing/import/boms/confirm")
    async def mfg_import_boms_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        # Build SKU -> item_id map (best effort)
        try:
            items = (await api.list_items(token, {"limit": 2000})).get("items", [])
        except APIError as e:
            logger.warning("API error fetching items for BOM import SKU map: %s", e.detail)
            items = []
        sku_to_id = {str(i.get("sku") or "").strip(): i.get("entity_id") for i in items if i.get("sku") and i.get("entity_id")}

        # Group components by bom_name
        boms: dict[str, dict] = {}
        for r in rows:
            name = str(r.get("bom_name", "")).strip()
            out_sku = str(r.get("output_sku", "")).strip()
            comp_sku = str(r.get("component_sku", "")).strip()
            if not name or not out_sku or not comp_sku:
                continue
            try:
                out_qty = float(str(r.get("output_qty", "1")).strip() or "1")
            except ValueError:
                out_qty = 1.0
            try:
                comp_qty = float(str(r.get("component_qty", "")).strip() or "0")
            except ValueError:
                comp_qty = 0.0

            bom = boms.setdefault(name, {"name": name, "output_item_id": sku_to_id.get(out_sku), "output_qty": out_qty, "components": []})
            bom["components"].append({
                "item_id": sku_to_id.get(comp_sku),
                "sku": comp_sku,
                "qty": comp_qty,
                "unit": str(r.get("unit", "pieces")).strip() or "pieces",
            })

        created = skipped = 0
        errors: list[str] = []
        for bom in boms.values():
            try:
                await api.create_bom(token, bom)
                created += 1
            except APIError as e:
                skipped += 1
                if len(errors) < 10:
                    errors.append(f"{bom.get('name')}: {e.detail}")

        return import_result_panel(
            created=created,
            skipped=skipped,
            errors=errors,
            entity_label="manufacturing",
            back_href="/manufacturing",
            import_more_href="/manufacturing/import/boms",
            has_mapping=True,
        )

    # ── Orders ───────────────────────────────────────────────────────────

    @app.get("/manufacturing/import/orders")
    async def mfg_import_orders_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header(
                "Import Manufacturing Orders",
                A(t("btn.back_to_settings"), href="/manufacturing", cls="btn btn--secondary"),
                A(t("btn.download_template"), href="/manufacturing/import/orders/template", cls="btn btn--secondary"),
            ),
            upload_form(
                cols=_ORDER_SPEC.cols,
                template_href="/manufacturing/import/orders/template",
                preview_action="/manufacturing/import/orders/preview",
                has_mapping=True,
            ),
            title="Import Manufacturing Orders - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.get("/manufacturing/import/orders/template")
    async def mfg_import_orders_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=_ORDER_SPEC.cols)
        w.writeheader()
        w.writerow({"description": "Assemble Ring", "order_type": "assembly", "bom_name": "Ring Assembly", "location_id": "", "due_date": "", "notes": ""})
        from starlette.responses import Response
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=mfg_orders_template.csv"},
        )

    @app.post("/manufacturing/import/orders/preview")
    async def mfg_import_orders_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Manufacturing Orders"),
                upload_form(
                    cols=_ORDER_SPEC.cols,
                    template_href="/manufacturing/import/orders/template",
                    preview_action="/manufacturing/import/orders/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Manufacturing Orders - Celerp",
                nav_active="manufacturing",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Manufacturing Orders"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_ORDER_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/manufacturing/import/orders/mapped",
                back_href="/manufacturing/import/orders",
                required_targets=_ORDER_SPEC.required,
            ),
            title="Import Manufacturing Orders - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/import/orders/mapped")
    async def mfg_import_orders_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Manufacturing Orders"),
                upload_form(
                    cols=_ORDER_SPEC.cols,
                    template_href="/manufacturing/import/orders/template",
                    preview_action="/manufacturing/import/orders/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Manufacturing Orders - Celerp",
                nav_active="manufacturing",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_ORDER_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Manufacturing Orders"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_ORDER_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/manufacturing/import/orders/mapped",
                    back_href="/manufacturing/import/orders",
                    required_targets=_ORDER_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Manufacturing Orders - Celerp",
                nav_active="manufacturing",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _ORDER_SPEC.cols)

        return base_shell(
            page_header("Import Manufacturing Orders"),
            validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v: validate_cell(_ORDER_SPEC, c, v),
                confirm_action="/manufacturing/import/orders/confirm",
                error_report_action="/manufacturing/import/orders/errors",
                back_href="/manufacturing/import/orders",
                revalidate_action="/manufacturing/import/orders/revalidate",
                has_mapping=True,
            ),
            title="Import Manufacturing Orders - Celerp",
            nav_active="manufacturing",
            request=request,
        )

    @app.post("/manufacturing/import/orders/revalidate")
    async def mfg_import_orders_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return upload_form(
                cols=_ORDER_SPEC.cols,
                template_href="/manufacturing/import/orders/template",
                preview_action="/manufacturing/import/orders/preview",
                has_mapping=True,
                error="CSV data expired. Please re-upload.",
            )
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _ORDER_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v: validate_cell(_ORDER_SPEC, c, v),
            confirm_action="/manufacturing/import/orders/confirm",
            error_report_action="/manufacturing/import/orders/errors",
            back_href="/manufacturing/import/orders",
            revalidate_action="/manufacturing/import/orders/revalidate",
            has_mapping=True,
        )

    @app.post("/manufacturing/import/orders/errors")
    async def mfg_import_orders_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        cols = list(rows[0].keys()) if rows else _ORDER_SPEC.cols
        return error_report_response(rows, cols, lambda c, v: validate_cell(_ORDER_SPEC, c, v), "orders_errors.csv")

    @app.post("/manufacturing/import/orders/confirm")
    async def mfg_import_orders_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        # Map bom_name -> bom_id by listing BOMs
        try:
            bom_items = (await api.list_boms(token)).get("items", [])
        except APIError as e:
            logger.warning("API error fetching BOMs for order import: %s", e.detail)
            bom_items = []
        bom_name_to_id = {str(b.get("name") or "").strip(): b.get("entity_id") for b in bom_items if b.get("name") and b.get("entity_id")}

        created = skipped = 0
        errors: list[str] = []

        for r in rows:
            desc = str(r.get("description", "")).strip()
            if not desc:
                skipped += 1
                continue
            bom_name = str(r.get("bom_name", "")).strip()
            payload = {
                "description": desc,
                "order_type": str(r.get("order_type", "assembly")).strip() or "assembly",
                "bom_id": bom_name_to_id.get(bom_name) if bom_name else None,
                "location_id": str(r.get("location_id", "")).strip() or None,
                "due_date": str(r.get("due_date", "")).strip() or None,
                "notes": str(r.get("notes", "")).strip() or None,
                "inputs": [],
                "expected_outputs": [],
                "idempotency_key": f"csv:mfg:{desc}".lower(),
            }
            try:
                await api.create_mfg_order(token, payload)
                created += 1
            except APIError as e:
                skipped += 1
                if len(errors) < 10:
                    errors.append(f"{desc}: {e.detail}")

        return import_result_panel(
            created=created,
            skipped=skipped,
            errors=errors,
            entity_label="manufacturing",
            back_href="/manufacturing",
            import_more_href="/manufacturing/import/orders",
            has_mapping=True,
        )
