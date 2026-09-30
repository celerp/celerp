# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import csv
import io
from typing import Any

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, page_title
from ui.config import get_token as _token
from ui.i18n import t
from ui.routes.csv_import import (
    CsvImportSpec,
    discard_import_csv,
    resolve_import_csv,
    _rows_to_csv,
    stash_import_csv,
    apply_column_mapping,
    apply_fixes_to_rows,
    column_mapping_form,
    error_report_response,
    import_result_errors,
    import_result_panel,
    rows_have_errors,
    stage_tabular_upload,
    upload_form,
    validate_cell,
    validate_column_mapping,
    validation_result,
)


# The accounting API owns this list (celerp_accounting.routes.ACCOUNT_TYPES) and
# validates against it; the two run in separate processes, so a test asserts they
# still match.
ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "cogs", "expense", "other")

_CHART_SPEC = CsvImportSpec(
    cols=["code", "name", "account_type", "parent_code", "is_active"],
    required={"code", "name", "account_type"},
    type_map={},
)


def _chart_validate(col: str, value: str, row: dict | None = None) -> bool:
    if col == "account_type" and value.strip():
        return value.strip() in ACCOUNT_TYPES
    if col == "is_active" and value.strip():
        return value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}
    return validate_cell(_CHART_SPEC, col, value)


def _chart_upload_form(error: str | None = None) -> FT:
    return upload_form(
        cols=_CHART_SPEC.cols,
        template_href="/accounting/import/chart/template",
        preview_action="/accounting/import/chart/preview",
        has_mapping=True,
        hint=t("accounting_import.chart_hint"),
        error=error,
    )


def _chart_records(rows: list[dict]) -> list[dict]:
    return [
        {
            "code": (r.get("code") or "").strip(),
            "name": (r.get("name") or "").strip(),
            "account_type": (r.get("account_type") or "").strip(),
            "parent_code": (r.get("parent_code") or "").strip() or None,
            "is_active": (r.get("is_active") or "").strip(),
        }
        for r in rows
    ]


def _kept_codes_note(kept: list[str]) -> FT | str:
    return Div(
        P(t("accounting_import.chart_hint")),
        P(f"{t('msg.skipped')}: {', '.join(kept)}"),
        cls="mt-sm",
    ) if kept else ""


def _chart_api_error_panel(e: APIError) -> FT:
    return import_result_panel(
        created=0,
        skipped=0,
        errors=[e.detail],
        entity_label=t("accounting_import.entity_accounts"),
        back_href="/settings/accounting?tab=chart",
        import_more_href="/accounting/import/chart",
        has_mapping=True,
    )


async def _chart_review(token: str, csv_ref: str, rows: list[dict], cols: list[str]) -> FT:
    """Cell fixes first; once every cell is valid, the import's own preview of
    which rows it would refuse and which codes it would keep."""
    notes: Any = ""
    if not rows_have_errors(rows, cols, _chart_validate):
        try:
            preview = await api.batch_import(token, "/accounting/accounts/import/preview", _chart_records(rows))
        except APIError as e:
            return _chart_api_error_panel(e)
        errors = import_result_errors(preview)
        notes = Div(
            Div(
                P(f"{len(errors)} {t('import.rows_need_changes')}"),
                Details(Summary(t("import.error_details", n=len(errors))), *(P(e) for e in errors), open=True),
                cls="flash flash--warning",
            ) if errors else "",
            _kept_codes_note([str(c) for c in preview.get("skipped_codes") or []]),
        )
    return validation_result(
        csv_ref=csv_ref,
        rows=rows,
        cols=cols,
        validate=_chart_validate,
        confirm_action="/accounting/import/chart/confirm",
        error_report_action="/accounting/import/chart/errors",
        back_href="/accounting/import/chart",
        revalidate_action="/accounting/import/chart/revalidate",
        has_mapping=True,
        notes=notes,
    )


def setup_routes(app):

    @app.get("/accounting/import/chart")
    async def import_chart_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await base_shell(
            page_header(t("accounting_import.header_chart")),
            _chart_upload_form(),
            title=page_title("accounting_import.title_chart"),
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
        rows, csv_ref, err = await stage_tabular_upload(token, form)
        if err:
            return await base_shell(
                page_header(t("accounting_import.header_chart")),
                _chart_upload_form(error=err),
                title=page_title("accounting_import.title_chart"),
                nav_active="accounting",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        return await base_shell(
            page_header(t("accounting_import.header_chart")),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_CHART_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/accounting/import/chart/mapped",
                back_href="/accounting/import/chart",
                required_targets=_CHART_SPEC.required,
            ),
            title=page_title("accounting_import.title_chart"),
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
        csv_text = await resolve_import_csv(token, form)
        if not csv_text:
            return await base_shell(
                page_header(t("accounting_import.header_chart")),
                _chart_upload_form(error=t("import.csv_expired")),
                title=page_title("accounting_import.title_chart"),
                nav_active="accounting",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_CHART_SPEC.cols), required_targets=_CHART_SPEC.required)
        if mapping_errors:
            csv_ref = await stash_import_csv(token, csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return await base_shell(
                page_header(t("accounting_import.header_chart")),
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
                title=page_title("accounting_import.title_chart"),
                nav_active="accounting",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = await stash_import_csv(token, remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _CHART_SPEC.cols)

        return await base_shell(
            page_header(t("accounting_import.header_chart")),
            await _chart_review(token, csv_ref, rows, cols),
            title=page_title("accounting_import.title_chart"),
            nav_active="accounting",
            request=request,
        )

    @app.post("/accounting/import/chart/revalidate")
    async def import_chart_revalidate(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = await resolve_import_csv(token, form)
        if not csv_data:
            return _chart_upload_form(error=t("import.csv_expired"))
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _CHART_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        csv_ref = await stash_import_csv(token, _rows_to_csv(rows, cols))
        return await _chart_review(token, csv_ref, rows, cols)

    @app.post("/accounting/import/chart/errors")
    async def import_chart_errors(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(await resolve_import_csv(token, form))))
        return error_report_response(rows, _CHART_SPEC.cols, _chart_validate, "chart_errors.csv")

    @app.post("/accounting/import/chart/confirm")
    async def import_chart_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)

        form = await request.form()
        csv_data = await resolve_import_csv(token, form)
        if not csv_data:
            return RedirectResponse("/accounting/import/chart", status_code=302)

        rows = list(csv.DictReader(io.StringIO(csv_data)))
        try:
            result = await api.batch_import(token, "/accounting/accounts/import/batch", _chart_records(rows))
        except APIError as e:
            return _chart_api_error_panel(e)

        await discard_import_csv(token, form, result)
        return import_result_panel(
            created=int(result.get("created", 0) or 0),
            skipped=int(result.get("skipped", 0) or 0),
            errors=import_result_errors(result),
            extra=_kept_codes_note([str(c) for c in result.get("skipped_codes") or []]),
            entity_label=t("accounting_import.entity_accounts"),
            back_href="/settings/accounting?tab=chart",
            import_more_href="/accounting/import/chart",
            has_mapping=True,
        )
