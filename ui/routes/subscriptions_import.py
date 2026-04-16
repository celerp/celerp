# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Subscriptions CSV import."""

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


_SUB_IMPORT_SPEC = CsvImportSpec(
    cols=[
        "name",
        "doc_type",
        "frequency",
        "start_date",
        "end_date",
        "contact_id",
        "payment_terms",
        "shipping",
        "discount",
        "tax",
    ],
    required={"name", "doc_type", "frequency", "start_date"},
    type_map={"shipping": float, "discount": float, "tax": float},
)


def setup_routes(app):

    @app.get("/subscriptions/import")
    async def subs_import_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header(
                "Import Subscriptions",
                A(t("btn.back_to_settings"), href="/subscriptions", cls="btn btn--secondary"),
                A(t("btn.download_template"), href="/subscriptions/import/template", cls="btn btn--secondary"),
            ),
            upload_form(
                cols=_SUB_IMPORT_SPEC.cols,
                template_href="/subscriptions/import/template",
                preview_action="/subscriptions/import/preview",
                has_mapping=True,
            ),
            title="Import Subscriptions - Celerp",
            nav_active="subscriptions",
            request=request,
        )

    @app.get("/subscriptions/import/template")
    async def subs_import_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=_SUB_IMPORT_SPEC.cols)
        w.writeheader()
        w.writerow({
            "name": "Monthly Retainer",
            "doc_type": "invoice",
            "frequency": "monthly",
            "start_date": "2026-01-01",
            "end_date": "",
            "contact_id": "",
            "payment_terms": "",
            "shipping": "0",
            "discount": "0",
            "tax": "0",
        })
        from starlette.responses import Response
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=subscriptions_template.csv"},
        )

    @app.post("/subscriptions/import/preview")
    async def subs_import_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Subscriptions"),
                upload_form(
                    cols=_SUB_IMPORT_SPEC.cols,
                    template_href="/subscriptions/import/template",
                    preview_action="/subscriptions/import/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Subscriptions - Celerp",
                nav_active="subscriptions",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Subscriptions"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_SUB_IMPORT_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/subscriptions/import/mapped",
                back_href="/subscriptions/import",
                required_targets=_SUB_IMPORT_SPEC.required,
            ),
            title="Import Subscriptions - Celerp",
            nav_active="subscriptions",
            request=request,
        )

    @app.post("/subscriptions/import/mapped")
    async def subs_import_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Subscriptions"),
                upload_form(
                    cols=_SUB_IMPORT_SPEC.cols,
                    template_href="/subscriptions/import/template",
                    preview_action="/subscriptions/import/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Subscriptions - Celerp",
                nav_active="subscriptions",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_SUB_IMPORT_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Subscriptions"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_SUB_IMPORT_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/subscriptions/import/mapped",
                    back_href="/subscriptions/import",
                    required_targets=_SUB_IMPORT_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Subscriptions - Celerp",
                nav_active="subscriptions",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _SUB_IMPORT_SPEC.cols)

        return base_shell(
            page_header("Import Subscriptions"),
            validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v: validate_cell(_SUB_IMPORT_SPEC, c, v),
                confirm_action="/subscriptions/import/confirm",
                error_report_action="/subscriptions/import/errors",
                back_href="/subscriptions/import",
                revalidate_action="/subscriptions/import/revalidate",
                has_mapping=True,
                upsert_label="name + start date",
            ),
            title="Import Subscriptions - Celerp",
            nav_active="subscriptions",
            request=request,
        )

    @app.post("/subscriptions/import/revalidate")
    async def subs_import_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return upload_form(
                cols=_SUB_IMPORT_SPEC.cols,
                template_href="/subscriptions/import/template",
                preview_action="/subscriptions/import/preview",
                has_mapping=True,
                error="CSV data expired. Please re-upload.",
            )
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _SUB_IMPORT_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v: validate_cell(_SUB_IMPORT_SPEC, c, v),
            confirm_action="/subscriptions/import/confirm",
            error_report_action="/subscriptions/import/errors",
            back_href="/subscriptions/import",
            revalidate_action="/subscriptions/import/revalidate",
            has_mapping=True,
            upsert_label="name + start date",
        )

    @app.post("/subscriptions/import/errors")
    async def subs_import_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        cols = list(rows[0].keys()) if rows else _SUB_IMPORT_SPEC.cols
        return error_report_response(rows, cols, lambda c, v: validate_cell(_SUB_IMPORT_SPEC, c, v), "subscriptions_errors.csv")

    @app.post("/subscriptions/import/confirm")
    async def subs_import_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        created = skipped = 0
        errors: list[str] = []

        for r in rows:
            name = str(r.get("name", "")).strip()
            doc_type = str(r.get("doc_type", "")).strip() or "invoice"
            frequency = str(r.get("frequency", "")).strip() or "monthly"
            start_date = str(r.get("start_date", "")).strip()
            if not name or not start_date:
                skipped += 1
                continue

            def _f(key: str) -> float:
                raw = str(r.get(key, "")).strip()
                if not raw:
                    return 0.0
                try:
                    return float(raw)
                except ValueError:
                    return 0.0

            payload = {
                "name": name,
                "doc_type": doc_type,
                "frequency": frequency,
                "custom_interval_days": None,
                "start_date": start_date,
                "end_date": str(r.get("end_date", "")).strip() or None,
                "contact_id": str(r.get("contact_id", "")).strip() or None,
                "payment_terms": str(r.get("payment_terms", "")).strip() or None,
                "shipping": _f("shipping"),
                "discount": _f("discount"),
                "tax": _f("tax"),
                "line_items": [],
                "idempotency_key": f"csv:sub:{name}:{start_date}".lower(),
            }

            try:
                await api.create_subscription(token, payload)
                created += 1
            except APIError as e:
                if len(errors) < 10:
                    errors.append(f"{name}: {e.detail}")

        return import_result_panel(
            created=created,
            skipped=skipped,
            updated=0,
            errors=errors,
            entity_label="subscriptions",
            back_href="/subscriptions",
            import_more_href="/subscriptions/import",
            has_mapping=True,
        )
