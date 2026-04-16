# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import csv
import io

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

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


_CHART_SPEC = CsvImportSpec(
    cols=["code", "name", "account_type", "parent_code", "is_active"],
    required={"code", "name", "account_type"},
    type_map={},
)


def _chart_validate(col: str, value: str) -> bool:
    if col == "account_type" and value.strip():
        return value.strip() in {"asset", "liability", "equity", "revenue", "expense", "cogs", "other"}
    if col == "is_active" and value.strip():
        return value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}
    return validate_cell(_CHART_SPEC, col, value)


def setup_routes(app):

    @app.get("/accounting/import/chart")
    async def import_chart_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return base_shell(
            page_header("Import Chart of Accounts"),
            upload_form(
                cols=_CHART_SPEC.cols,
                template_href="/accounting/import/chart/template",
                preview_action="/accounting/import/chart/preview",
                has_mapping=True,
                hint="Upload a CSV to add accounts to your chart. Existing codes will be skipped.",
            ),
            title="Import Chart - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.get("/accounting/import/chart/template")
    async def import_chart_template(request: Request):
        _ = _token(request)
        header = ",".join(_CHART_SPEC.cols) + "\n"
        example = "1000,Assets,asset,,true\n"
        return PlainTextResponse(header + example, media_type="text/csv")

    @app.post("/accounting/import/chart/preview")
    async def import_chart_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Chart of Accounts"),
                upload_form(
                    cols=_CHART_SPEC.cols,
                    template_href="/accounting/import/chart/template",
                    preview_action="/accounting/import/chart/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Chart - Celerp",
                nav_active="accounting",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Chart of Accounts"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_CHART_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/accounting/import/chart/mapped",
                back_href="/accounting/import/chart",
                required_targets=_CHART_SPEC.required,
            ),
            title="Import Chart - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/import/chart/mapped")
    async def import_chart_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Chart of Accounts"),
                upload_form(
                    cols=_CHART_SPEC.cols,
                    template_href="/accounting/import/chart/template",
                    preview_action="/accounting/import/chart/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Chart - Celerp",
                nav_active="accounting",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_CHART_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Chart of Accounts"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_CHART_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/accounting/import/chart/mapped",
                    back_href="/accounting/import/chart",
                    required_targets=_CHART_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Chart - Celerp",
                nav_active="accounting",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _CHART_SPEC.cols)

        return base_shell(
            page_header("Import Chart of Accounts"),
            validation_result(
                rows=rows,
                cols=cols,
                validate=_chart_validate,
                confirm_action="/accounting/import/chart/confirm",
                error_report_action="/accounting/import/chart/errors",
                back_href="/accounting/import/chart",
                revalidate_action="/accounting/import/chart/revalidate",
                has_mapping=True,
            ),
            title="Import Chart - Celerp",
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/import/chart/revalidate")
    async def import_chart_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return upload_form(
                cols=_CHART_SPEC.cols,
                template_href="/accounting/import/chart/template",
                preview_action="/accounting/import/chart/preview",
                has_mapping=True,
                error="CSV data expired. Please re-upload.",
            )
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _CHART_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return validation_result(
            rows=rows, cols=cols,
            validate=_chart_validate,
            confirm_action="/accounting/import/chart/confirm",
            error_report_action="/accounting/import/chart/errors",
            back_href="/accounting/import/chart",
            revalidate_action="/accounting/import/chart/revalidate",
            has_mapping=True,
        )

    @app.post("/accounting/import/chart/errors")
    async def import_chart_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        return error_report_response(rows, _CHART_SPEC.cols, _chart_validate, "chart_errors.csv")

    @app.post("/accounting/import/chart/confirm")
    async def import_chart_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/accounting/import/chart", status_code=302)

        rows = list(csv.DictReader(io.StringIO(csv_data)))
        records = [
            {
                "code": (r.get("code") or "").strip(),
                "name": (r.get("name") or "").strip(),
                "account_type": (r.get("account_type") or "").strip(),
                "parent_code": (r.get("parent_code") or "").strip() or None,
                "is_active": (r.get("is_active") or "").strip(),
            }
            for r in rows
        ]

        try:
            result = await api.batch_import(token, "/accounting/accounts/import/batch", records)
        except APIError as e:
            return import_result_panel(
                created=0,
                skipped=0,
                errors=[e.detail],
                entity_label="accounts",
                back_href="/accounting?tab=chart",
                import_more_href="/accounting/import/chart",
                has_mapping=True,
            )

        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        errors = [f"{failed} record(s) failed"] if failed else []

        return import_result_panel(
            created=created,
            skipped=skipped,
            errors=errors,
            entity_label="accounts",
            back_href="/accounting?tab=chart",
            import_more_href="/accounting/import/chart",
            has_mapping=True,
        )
