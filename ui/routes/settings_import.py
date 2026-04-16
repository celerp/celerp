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
from ui.routes.settings import _check_role
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


_LOCATION_SPEC = CsvImportSpec(
    cols=["name", "type"],
    required={"name", "type"},
    type_map={},
)

_TAX_SPEC = CsvImportSpec(
    cols=["name", "rate", "tax_type", "is_default", "description"],
    required={"name", "rate"},
    type_map={"rate": float},
)

_TERMS_SPEC = CsvImportSpec(
    cols=["name", "days", "description"],
    required={"name", "days"},
    type_map={"days": int},
)


def _loc_validate(col: str, value: str) -> bool:
    return validate_cell(_LOCATION_SPEC, col, value)


def _tax_validate(col: str, value: str) -> bool:
    if col == "tax_type" and value.strip():
        return value.strip() in {"sales", "purchase", "both"}
    if col == "is_default" and value.strip():
        return value.strip().lower() in {"true", "false", "1", "0", "yes", "no"}
    return validate_cell(_TAX_SPEC, col, value)


def _terms_validate(col: str, value: str) -> bool:
    return validate_cell(_TERMS_SPEC, col, value)


def setup_routes(app):

    # ── Locations ───────────────────────────────────────────────

    @app.get("/settings/import/locations")
    async def import_locations_page(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        return base_shell(
            page_header("Import Locations"),
            upload_form(
                cols=_LOCATION_SPEC.cols,
                template_href="/settings/import/locations/template",
                preview_action="/settings/import/locations/preview",
                has_mapping=True,
                hint="Upload locations. Existing names will be skipped.",
            ),
            title="Import Locations - Celerp",
            nav_active="settings",
            request=request,
        )

    @app.get("/settings/import/locations/template")
    async def import_locations_template(request: Request):
        header = ",".join(_LOCATION_SPEC.cols) + "\n"
        return PlainTextResponse(header + "Main Store,store\n", media_type="text/csv")

    @app.post("/settings/import/locations/preview")
    async def import_locations_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Locations"),
                upload_form(
                    cols=_LOCATION_SPEC.cols,
                    template_href="/settings/import/locations/template",
                    preview_action="/settings/import/locations/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Locations - Celerp", nav_active="settings",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Locations"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_LOCATION_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/settings/import/locations/mapped",
                back_href="/settings/import/locations",
                required_targets=_LOCATION_SPEC.required,
            ),
            title="Import Locations - Celerp", nav_active="settings",
            request=request,
        )

    @app.post("/settings/import/locations/mapped")
    async def import_locations_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Locations"),
                upload_form(
                    cols=_LOCATION_SPEC.cols,
                    template_href="/settings/import/locations/template",
                    preview_action="/settings/import/locations/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Locations - Celerp", nav_active="settings",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_LOCATION_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Locations"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_LOCATION_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/settings/import/locations/mapped",
                    back_href="/settings/import/locations",
                    required_targets=_LOCATION_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Locations - Celerp", nav_active="settings",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _LOCATION_SPEC.cols)

        return base_shell(
            page_header("Import Locations"),
            validation_result(
                rows=rows, cols=cols, validate=_loc_validate,
                confirm_action="/settings/import/locations/confirm",
                error_report_action="/settings/import/locations/errors",
                back_href="/settings/import/locations",
                revalidate_action="/settings/import/locations/revalidate",
                has_mapping=True,
            ),
            title="Import Locations - Celerp", nav_active="settings",
            request=request,
        )

    @app.post("/settings/import/locations/revalidate")
    async def import_locations_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/settings/import/locations", status_code=302)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        rows = apply_fixes_to_rows(form, rows, _LOCATION_SPEC.cols)
        _stash_csv(_rows_to_csv(rows, _LOCATION_SPEC.cols))
        return validation_result(
            rows=rows, cols=_LOCATION_SPEC.cols, validate=_loc_validate,
            confirm_action="/settings/import/locations/confirm",
            error_report_action="/settings/import/locations/errors",
            back_href="/settings/import/locations",
            revalidate_action="/settings/import/locations/revalidate",
            has_mapping=True,
        )

    @app.post("/settings/import/locations/errors")
    async def import_locations_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        return error_report_response(rows, _LOCATION_SPEC.cols, _loc_validate, "locations_errors.csv")

    @app.post("/settings/import/locations/confirm")
    async def import_locations_confirm(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        token = _token(request)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/settings/import/locations", status_code=302)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        records = [{"name": (r.get("name") or "").strip(), "type": (r.get("type") or "").strip()} for r in rows]
        try:
            result = await api.batch_import(token, "/companies/me/locations/import/batch", records)
        except APIError as e:
            return import_result_panel(
                created=0, skipped=0, errors=[e.detail],
                entity_label="locations",
                back_href="/settings/inventory?tab=locations",
                import_more_href="/settings/import/locations",
                has_mapping=True,
            )
        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        errors = [f"{failed} record(s) failed"] if failed else []
        return import_result_panel(
            created=created, skipped=skipped, errors=errors,
            entity_label="locations",
            back_href="/settings/inventory?tab=locations",
            import_more_href="/settings/import/locations",
            has_mapping=True,
        )

    # ── Taxes ───────────────────────────────────────────────────

    @app.get("/settings/import/taxes")
    async def import_taxes_page(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        return base_shell(
            page_header("Import Taxes"),
            upload_form(
                cols=_TAX_SPEC.cols,
                template_href="/settings/import/taxes/template",
                preview_action="/settings/import/taxes/preview",
                has_mapping=True,
                hint="Upload tax rates. Existing names will be skipped.",
            ),
            title="Import Taxes - Celerp", nav_active="settings",
            request=request,
        )

    @app.get("/settings/import/taxes/template")
    async def import_taxes_template(request: Request):
        header = ",".join(_TAX_SPEC.cols) + "\n"
        return PlainTextResponse(header + "VAT 7%,7,both,true,Thailand standard VAT\n", media_type="text/csv")

    @app.post("/settings/import/taxes/preview")
    async def import_taxes_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Taxes"),
                upload_form(
                    cols=_TAX_SPEC.cols,
                    template_href="/settings/import/taxes/template",
                    preview_action="/settings/import/taxes/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Taxes - Celerp", nav_active="settings",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Taxes"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_TAX_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/settings/import/taxes/mapped",
                back_href="/settings/import/taxes",
                required_targets=_TAX_SPEC.required,
            ),
            title="Import Taxes - Celerp", nav_active="settings",
            request=request,
        )

    @app.post("/settings/import/taxes/mapped")
    async def import_taxes_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Taxes"),
                upload_form(
                    cols=_TAX_SPEC.cols,
                    template_href="/settings/import/taxes/template",
                    preview_action="/settings/import/taxes/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Taxes - Celerp", nav_active="settings",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_TAX_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Taxes"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_TAX_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/settings/import/taxes/mapped",
                    back_href="/settings/import/taxes",
                    required_targets=_TAX_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Taxes - Celerp", nav_active="settings",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _TAX_SPEC.cols)

        return base_shell(
            page_header("Import Taxes"),
            validation_result(
                rows=rows, cols=cols, validate=_tax_validate,
                confirm_action="/settings/import/taxes/confirm",
                error_report_action="/settings/import/taxes/errors",
                back_href="/settings/import/taxes",
                revalidate_action="/settings/import/taxes/revalidate",
                has_mapping=True,
            ),
            title="Import Taxes - Celerp", nav_active="settings",
            request=request,
        )

    @app.post("/settings/import/taxes/revalidate")
    async def import_taxes_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/settings/import/taxes", status_code=302)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        rows = apply_fixes_to_rows(form, rows, _TAX_SPEC.cols)
        _stash_csv(_rows_to_csv(rows, _TAX_SPEC.cols))
        return validation_result(
            rows=rows, cols=_TAX_SPEC.cols, validate=_tax_validate,
            confirm_action="/settings/import/taxes/confirm",
            error_report_action="/settings/import/taxes/errors",
            back_href="/settings/import/taxes",
            revalidate_action="/settings/import/taxes/revalidate",
            has_mapping=True,
        )

    @app.post("/settings/import/taxes/errors")
    async def import_taxes_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        return error_report_response(rows, _TAX_SPEC.cols, _tax_validate, "taxes_errors.csv")

    @app.post("/settings/import/taxes/confirm")
    async def import_taxes_confirm(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        token = _token(request)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/settings/import/taxes", status_code=302)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        records = [{
            "name": (r.get("name") or "").strip(),
            "rate": (r.get("rate") or "").strip(),
            "tax_type": (r.get("tax_type") or "both").strip() or "both",
            "is_default": (r.get("is_default") or "").strip(),
            "description": (r.get("description") or "").strip(),
        } for r in rows]
        try:
            result = await api.batch_import(token, "/companies/me/taxes/import/batch", records)
        except APIError as e:
            return import_result_panel(
                created=0, skipped=0, errors=[e.detail],
                entity_label="taxes",
                back_href="/settings/sales?tab=taxes",
                import_more_href="/settings/import/taxes",
                has_mapping=True,
            )
        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        errors = [f"{failed} record(s) failed"] if failed else []
        return import_result_panel(
            created=created, skipped=skipped, errors=errors,
            entity_label="taxes",
            back_href="/settings/sales?tab=taxes",
            import_more_href="/settings/import/taxes",
            has_mapping=True,
        )

    # ── Payment Terms ─────────────────────────────────────────────

    @app.get("/settings/import/payment-terms")
    async def import_terms_page(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "manager")):
            return r
        return base_shell(
            page_header("Import Payment Terms"),
            upload_form(
                cols=_TERMS_SPEC.cols,
                template_href="/settings/import/payment-terms/template",
                preview_action="/settings/import/payment-terms/preview",
                has_mapping=True,
                hint="Upload payment terms. Existing names will be skipped.",
            ),
            title="Import Terms - Celerp", nav_active="settings",
            request=request,
        )

    @app.get("/settings/import/payment-terms/template")
    async def import_terms_template(request: Request):
        header = ",".join(_TERMS_SPEC.cols) + "\n"
        return PlainTextResponse(header + "Net 30,30,Due within 30 days\n", media_type="text/csv")

    @app.post("/settings/import/payment-terms/preview")
    async def import_terms_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return base_shell(
                page_header("Import Payment Terms"),
                upload_form(
                    cols=_TERMS_SPEC.cols,
                    template_href="/settings/import/payment-terms/template",
                    preview_action="/settings/import/payment-terms/preview",
                    has_mapping=True,
                    error=err,
                ),
                title="Import Terms - Celerp", nav_active="settings",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return base_shell(
            page_header("Import Payment Terms"),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_TERMS_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/settings/import/payment-terms/mapped",
                back_href="/settings/import/payment-terms",
                required_targets=_TERMS_SPEC.required,
            ),
            title="Import Terms - Celerp", nav_active="settings",
            request=request,
        )

    @app.post("/settings/import/payment-terms/mapped")
    async def import_terms_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return base_shell(
                page_header("Import Payment Terms"),
                upload_form(
                    cols=_TERMS_SPEC.cols,
                    template_href="/settings/import/payment-terms/template",
                    preview_action="/settings/import/payment-terms/preview",
                    has_mapping=True,
                    error="CSV data expired. Please re-upload.",
                ),
                title="Import Terms - Celerp", nav_active="settings",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_TERMS_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return base_shell(
                page_header("Import Payment Terms"),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_TERMS_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/settings/import/payment-terms/mapped",
                    back_href="/settings/import/payment-terms",
                    required_targets=_TERMS_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title="Import Terms - Celerp", nav_active="settings",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _TERMS_SPEC.cols)

        return base_shell(
            page_header("Import Payment Terms"),
            validation_result(
                rows=rows, cols=cols, validate=_terms_validate,
                confirm_action="/settings/import/payment-terms/confirm",
                error_report_action="/settings/import/payment-terms/errors",
                back_href="/settings/import/payment-terms",
                revalidate_action="/settings/import/payment-terms/revalidate",
                has_mapping=True,
            ),
            title="Import Terms - Celerp", nav_active="settings",
            request=request,
        )

    @app.post("/settings/import/payment-terms/revalidate")
    async def import_terms_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/settings/import/payment-terms", status_code=302)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        rows = apply_fixes_to_rows(form, rows, _TERMS_SPEC.cols)
        _stash_csv(_rows_to_csv(rows, _TERMS_SPEC.cols))
        return validation_result(
            rows=rows, cols=_TERMS_SPEC.cols, validate=_terms_validate,
            confirm_action="/settings/import/payment-terms/confirm",
            error_report_action="/settings/import/payment-terms/errors",
            back_href="/settings/import/payment-terms",
            revalidate_action="/settings/import/payment-terms/revalidate",
            has_mapping=True,
        )

    @app.post("/settings/import/payment-terms/errors")
    async def import_terms_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows = list(csv.DictReader(io.StringIO(_resolve_csv_text(form))))
        return error_report_response(rows, _TERMS_SPEC.cols, _terms_validate, "payment_terms_errors.csv")

    @app.post("/settings/import/payment-terms/confirm")
    async def import_terms_confirm(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        token = _token(request)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return RedirectResponse("/settings/import/payment-terms", status_code=302)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        records = [{
            "name": (r.get("name") or "").strip(),
            "days": (r.get("days") or "").strip(),
            "description": (r.get("description") or "").strip(),
        } for r in rows]
        try:
            result = await api.batch_import(token, "/companies/me/payment-terms/import/batch", records)
        except APIError as e:
            return import_result_panel(
                created=0, skipped=0, errors=[e.detail],
                entity_label="payment terms",
                back_href="/settings/sales?tab=terms",
                import_more_href="/settings/import/payment-terms",
                has_mapping=True,
            )
        created = int(result.get("created", 0) or 0)
        skipped = int(result.get("skipped", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        errors = [f"{failed} record(s) failed"] if failed else []
        return import_result_panel(
            created=created, skipped=skipped, errors=errors,
            entity_label="payment terms",
            back_href="/settings/sales?tab=terms",
            import_more_href="/settings/import/payment-terms",
            has_mapping=True,
        )
