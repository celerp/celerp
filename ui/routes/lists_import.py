# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Lists CSV import."""

from __future__ import annotations

import csv
import io
import uuid

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


_LIST_IMPORT_SPEC = CsvImportSpec(
    cols=["ref_id", "status", "total", "total_weight", "notes"],
    required={"ref_id"},
    type_map={"total": float, "total_weight": float},
)


def setup_routes(app):

    @app.get("/lists/import")
    async def lists_import_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header(
                "Import Lists",
                A(t("btn.back_to_settings"), href="/lists", cls="btn btn--secondary"),
                A(t("btn.download_template"), href="/lists/import/template", cls="btn btn--secondary"),
            ),
            upload_form(
                cols=_LIST_IMPORT_SPEC.cols,
                template_href="/lists/import/template",
                preview_action="/lists/import/preview",
                has_mapping=True,
            ),
            title="Import Lists - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.get("/lists/import/template")
    async def lists_import_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=_LIST_IMPORT_SPEC.cols)
        w.writeheader()
        w.writerow({"ref_id": "L-0001", "status": "draft", "total": "0", "total_weight": "0", "notes": ""})
        from starlette.responses import Response
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=lists_template.csv"},
        )

    @app.post("/lists/import/preview")
    async def lists_import_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Lists"),
                upload_form(
                    cols=_LIST_IMPORT_SPEC.cols,
                    template_href="/lists/import/template",
                    preview_action="/lists/import/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Lists - Celerp",
                nav_active="lists",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Lists"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_LIST_IMPORT_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/lists/import/mapped",
                back_href="/lists/import",
                required_targets=_LIST_IMPORT_SPEC.required,
            ),
            title="Import Lists - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.post("/lists/import/mapped")
    async def lists_import_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Lists"),
                upload_form(
                    cols=_LIST_IMPORT_SPEC.cols,
                    template_href="/lists/import/template",
                    preview_action="/lists/import/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Lists - Celerp",
                nav_active="lists",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_LIST_IMPORT_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Lists"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_LIST_IMPORT_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/lists/import/mapped",
                    back_href="/lists/import",
                    required_targets=_LIST_IMPORT_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Lists - Celerp",
                nav_active="lists",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _LIST_IMPORT_SPEC.cols)

        return base_shell(
            page_header("Import Lists"),
            validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v: validate_cell(_LIST_IMPORT_SPEC, c, v),
                confirm_action="/lists/import/confirm",
                error_report_action="/lists/import/errors",
                back_href="/lists/import",
                revalidate_action="/lists/import/revalidate",
                has_mapping=True,
                upsert_label="ref ID",
            ),
            title="Import Lists - Celerp",
            nav_active="lists",
            request=request,
        )

    @app.post("/lists/import/revalidate")
    async def lists_import_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return upload_form(
                cols=_LIST_IMPORT_SPEC.cols,
                template_href="/lists/import/template",
                preview_action="/lists/import/preview",
                has_mapping=True,
                error="CSV data expired. Please re-upload.",
            )
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _LIST_IMPORT_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v: validate_cell(_LIST_IMPORT_SPEC, c, v),
            confirm_action="/lists/import/confirm",
            error_report_action="/lists/import/errors",
            back_href="/lists/import",
            revalidate_action="/lists/import/revalidate",
            has_mapping=True,
            upsert_label="ref ID",
        )

    @app.post("/lists/import/errors")
    async def lists_import_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        cols = list(rows[0].keys()) if rows else _LIST_IMPORT_SPEC.cols
        return error_report_response(rows, cols, lambda c, v: validate_cell(_LIST_IMPORT_SPEC, c, v), "lists_errors.csv")

    @app.post("/lists/import/confirm")
    async def lists_import_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        upsert = form.get("upsert") == "1"
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        records: list[dict] = []
        for r in rows:
            ref_id = str(r.get("ref_id", "")).strip()
            if not ref_id:
                continue

            def _f(key: str) -> float | None:
                raw = str(r.get(key, "")).strip()
                if not raw:
                    return None
                try:
                    return float(raw)
                except ValueError:
                    return None

            data = {
                "ref_id": ref_id,
                "status": (str(r.get("status", "")).strip() or "draft").lower(),
                "total": _f("total") or 0.0,
                "total_weight": _f("total_weight") or 0.0,
                "notes": str(r.get("notes", "")).strip() or None,
            }
            idem = f"csv:list:{ref_id}".lower()
            records.append({
                "entity_id": f"list:{uuid.uuid4()}",
                "event_type": "list.created",
                "data": data,
                "source": "csv_import",
                "idempotency_key": idem,
            })

        try:
            result = await api.batch_import(token, "/lists/import/batch", records, upsert=upsert)
        except APIError as e:
            if e.status == 401:
                return RedirectResponse("/login", status_code=302)
            result = {"created": 0, "skipped": 0, "errors": [e.detail]}

        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        updated = int(result.get("updated", 0) or 0)
        errors = list(result.get("errors", []) or [])

        return import_result_panel(
            created=created,
            skipped=skipped,
            updated=updated,
            errors=errors,
            entity_label="lists",
            back_href="/lists",
            import_more_href="/lists/import",
            has_mapping=True,
        )
