# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Documents CSV import (Invoices, POs, Quotes, etc.)."""

from __future__ import annotations

import csv
import io
import uuid
from urllib.parse import parse_qs, urlsplit

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import base_shell, page_header, page_title
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


_DOC_IMPORT_SPEC = CsvImportSpec(
    cols=[
        "doc_type", "doc_number", "date", "due_date",
        "contact_name", "total", "amount_outstanding", "status",
        "line_sku", "line_barcode", "line_stone_type",
        "line_weight_ct", "line_qty", "line_unit_price", "line_total_price", "line_cost_basis",
    ],
    required={"doc_type", "doc_number"},
    type_map={"total": float, "amount_outstanding": float, "line_total_price": float,
               "line_unit_price": float, "line_weight_ct": float, "line_qty": float},
)


# The address free-tier share links are published under.
_FREE_SHARE_HOST = "share.celerp.com"


def parse_share_link(link: str) -> str | None:
    """Return the share page URL for a Celerp share link, or None.

    Accepts every form a sender can pass on: the celerp.com accept link
    (``...accept?src=<url>&token=<token>``), the sender's own share page
    (``<url>/share/<token>``), and a free-tier ``share.celerp.com`` link.
    """
    parts = urlsplit(link.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    query = parse_qs(parts.query)
    if query.get("src") and query.get("token"):
        return f"{query['src'][0].rstrip('/')}/share/{query['token'][0]}"
    path = parts.path.rstrip("/")
    base, sep, share_token = path.partition("/share/")
    if sep and share_token and "/" not in share_token:
        return f"{parts.scheme}://{parts.netloc}{path}"
    segment = path.lstrip("/")
    if parts.hostname == _FREE_SHARE_HOST and segment and "/" not in segment:
        return f"{parts.scheme}://{parts.netloc}/{segment}"
    return None


def shared_import_panel(*, error: str | None = None, link: str = "") -> FT:
    """Import a document someone shared: paste the link, or upload the .celerp file."""
    return Div(
        H2(t("docs_import.shared_title"), cls="section-title"),
        P(t("docs_import.shared_hint"), cls="text-muted"),
        P(error, cls="flash flash--error") if error else "",
        Form(
            Div(
                Label(t("docs_import.share_link_label"), For="share-link", cls="form-label"),
                Input(type="url", id="share-link", name="link", value=link, required=True, cls="form-input"),
                cls="form-group",
            ),
            Button(t("btn.import"), cls="btn btn--primary", type="submit"),
            method="post",
            action="/docs/import/shared",
        ),
        P(t("docs_import.celerp_file_hint"), cls="text-muted", style="margin-top: 16px;"),
        Form(
            Div(
                Label(t("docs_import.celerp_file_label"), For="celerp-file", cls="form-label"),
                Input(type="file", id="celerp-file", name="bundle", accept=".celerp,application/json", required=True),
                cls="form-group",
            ),
            Button(t("btn.import"), cls="btn btn--secondary", type="submit"),
            method="post",
            action="/docs/import/shared-file",
            enctype="multipart/form-data",
        ),
        cls="import-panel",
        id="shared-import",
        style="margin-top: 32px;",
    )


def setup_routes(app):

    async def _import_page(request: Request, *, shared_error: str | None = None, link: str = ""):
        return await base_shell(
            page_header(
                t("docs_import.import_documents"),
                A(t("btn.back_to_settings"), href="/docs", cls="btn btn--secondary"),
                A(t("btn.download_template"), href="/docs/import/template", cls="btn btn--secondary"),
            ),
            upload_form(
                cols=_DOC_IMPORT_SPEC.cols,
                template_href="/docs/import/template",
                preview_action="/docs/import/preview",
                has_mapping=True,
            ),
            shared_import_panel(error=shared_error, link=link),
            title=page_title("docs_import.import_documents"),
            nav_active="docs",
            request=request,
        )

    @app.get("/docs/import")
    async def docs_import_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await _import_page(request)

    @app.post("/docs/import/shared")
    async def docs_import_shared(request: Request):
        """Import a document another Celerp shared, from its share link."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        link = str((await request.form()).get("link") or "").strip()
        share_page = parse_share_link(link)
        if share_page is None:
            return await _import_page(request, shared_error=t("docs_import.invalid_share_link"), link=link)
        try:
            doc_path = await api.import_shared_doc(token, share_page)
        except APIError as e:
            return await _import_page(request, shared_error=str(e.detail), link=link)
        return RedirectResponse(doc_path, status_code=303)

    @app.post("/docs/import/shared-file")
    async def docs_import_shared_file(request: Request):
        """Import a document from a downloaded .celerp file."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        upload = (await request.form()).get("bundle")
        if not getattr(upload, "size", None):
            return await _import_page(request, shared_error=t("docs_import.no_celerp_file"))
        try:
            # Streamed on to the API, which enforces the bundle size limit.
            doc_path = await api.import_doc_bundle(token, upload.filename or "shared.celerp", upload.file)
        except APIError as e:
            return await _import_page(request, shared_error=str(e.detail))
        return RedirectResponse(doc_path, status_code=303)

    @app.get("/docs/import/template")
    async def docs_import_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=_DOC_IMPORT_SPEC.cols)
        w.writeheader()
        w.writerow({
            "doc_type": "invoice",
            "doc_number": "INV-0001",
            "date": "2026-01-31",
            "due_date": "2026-02-28",
            "contact_name": "Acme Corp",
            "total": "1500",
            "amount_outstanding": "1500",
            "status": "unpaid",
            "line_sku": "SKU-001",
            "line_barcode": "",
            "line_stone_type": "Emerald",
            "line_weight_ct": "2.5",
            "line_qty": "1",
            "line_unit_price": "1500",
            "line_total_price": "1500",
            "line_cost_basis": "800",
        })
        from starlette.responses import Response
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=documents_template.csv"},
        )

    @app.post("/docs/import/preview")
    async def docs_import_preview(request: Request):
        """Step 1: Upload CSV -> show column mapping form."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, err = await read_csv_upload(form)
        if err:
            return await base_shell(
                page_header(t("docs_import.import_documents")),
                upload_form(
                    cols=_DOC_IMPORT_SPEC.cols,
                    template_href="/docs/import/template",
                    preview_action="/docs/import/preview",
                    has_mapping=True,
                    error=err,
                ),
                title=page_title("docs_import.import_documents"),
                nav_active="docs",
                request=request,
            )
        cols = list(rows[0].keys()) if rows else []
        csv_text = _rows_to_csv(rows, cols)
        csv_ref = _stash_csv(csv_text)
        return await base_shell(
            page_header(t("docs_import.import_documents")),
            column_mapping_form(
                csv_cols=cols,
                target_cols=_DOC_IMPORT_SPEC.cols,
                csv_ref=csv_ref,
                sample_rows=rows,
                confirm_action="/docs/import/mapped",
                back_href="/docs/import",
                required_targets=_DOC_IMPORT_SPEC.required,
            ),
            title=page_title("docs_import.import_documents"),
            nav_active="docs",
            request=request,
        )

    @app.post("/docs/import/mapped")
    async def docs_import_mapped(request: Request):
        """Step 2: Apply column mapping -> validate -> show preview."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_text = _resolve_csv_text(form)
        if not csv_text:
            return await base_shell(
                page_header(t("docs_import.import_documents")),
                upload_form(
                    cols=_DOC_IMPORT_SPEC.cols,
                    template_href="/docs/import/template",
                    preview_action="/docs/import/preview",
                    has_mapping=True,
                    error=t("import.csv_expired"),
                ),
                title=page_title("docs_import.import_documents"),
                nav_active="docs",
                request=request,
            )

        original_cols = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        mapping_errors = validate_column_mapping(form, original_cols, core_fields=set(_DOC_IMPORT_SPEC.cols))
        if mapping_errors:
            csv_ref = _stash_csv(csv_text)
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            return await base_shell(
                page_header(t("docs_import.import_documents")),
                column_mapping_form(
                    csv_cols=original_cols,
                    target_cols=_DOC_IMPORT_SPEC.cols,
                    csv_ref=csv_ref,
                    sample_rows=rows,
                    confirm_action="/docs/import/mapped",
                    back_href="/docs/import",
                    required_targets=_DOC_IMPORT_SPEC.required,
                    errors=mapping_errors,
                    form_values=dict(form),
                ),
                title=page_title("docs_import.import_documents"),
                nav_active="docs",
                request=request,
            )

        remapped_csv, remapped_cols = apply_column_mapping(form, csv_text)
        csv_ref = _stash_csv(remapped_csv)
        rows = list(csv.DictReader(io.StringIO(remapped_csv)))
        cols = remapped_cols or (list(rows[0].keys()) if rows else _DOC_IMPORT_SPEC.cols)

        return await base_shell(
            page_header(t("docs_import.import_documents")),
            validation_result(
                rows=rows,
                cols=cols,
                validate=lambda c, v, r: validate_cell(_DOC_IMPORT_SPEC, c, v),
                confirm_action="/docs/import/confirm",
                error_report_action="/docs/import/errors",
                back_href="/docs/import",
                revalidate_action="/docs/import/revalidate",
                has_mapping=True,
                upsert_label=t("docs_import.upsert_label"),
            ),
            title=page_title("docs_import.import_documents"),
            nav_active="docs",
            request=request,
        )

    @app.post("/docs/import/revalidate")
    async def docs_import_revalidate(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        if not csv_data:
            return upload_form(
                cols=_DOC_IMPORT_SPEC.cols,
                template_href="/docs/import/template",
                preview_action="/docs/import/preview",
                has_mapping=True,
                error=t("import.csv_expired"),
            )
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _DOC_IMPORT_SPEC.cols
        rows = apply_fixes_to_rows(form, rows, cols)
        _stash_csv(_rows_to_csv(rows, cols))
        return validation_result(
            rows=rows, cols=cols,
            validate=lambda c, v, r: validate_cell(_DOC_IMPORT_SPEC, c, v),
            confirm_action="/docs/import/confirm",
            error_report_action="/docs/import/errors",
            back_href="/docs/import",
            revalidate_action="/docs/import/revalidate",
            has_mapping=True,
            upsert_label=t("docs_import.upsert_label"),
        )

    @app.post("/docs/import/errors")
    async def docs_import_errors(request: Request):
        if not _token(request):
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))
        cols = list(rows[0].keys()) if rows else _DOC_IMPORT_SPEC.cols
        return error_report_response(rows, cols, lambda c, v: validate_cell(_DOC_IMPORT_SPEC, c, v), "documents_errors.csv")

    @app.post("/docs/import/confirm")
    async def docs_import_confirm(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        upsert = form.get("upsert") == "1"
        csv_data = _resolve_csv_text(form)
        rows = list(csv.DictReader(io.StringIO(csv_data)))

        def _f(row: dict, key: str) -> float | None:
            raw = str(row.get(key, "")).strip()
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        # Group rows by (doc_type, doc_number) - one CSV row per line item
        from collections import OrderedDict
        doc_map: OrderedDict = OrderedDict()
        for r in rows:
            doc_type = str(r.get("doc_type", "")).strip()
            doc_number = str(r.get("doc_number", "")).strip()
            if not doc_type or not doc_number:
                continue
            key = (doc_type, doc_number)
            if key not in doc_map:
                doc_map[key] = {
                    "doc_type": doc_type,
                    "doc_number": doc_number,
                    "date": str(r.get("date", "")).strip() or None,
                    "due_date": str(r.get("due_date", "")).strip() or None,
                    "contact_name": str(r.get("contact_name", "")).strip() or None,
                    "total": _f(r, "total") or 0.0,
                    "amount_outstanding": _f(r, "amount_outstanding") or 0.0,
                    "status": str(r.get("status", "")).strip() or "draft",
                    "line_items": [],
                }
            # Append line item if present
            line_sku = str(r.get("line_sku", "")).strip()
            line_total = _f(r, "line_total_price")
            if line_sku or line_total:
                line: dict = {}
                if line_sku:
                    line["sku"] = line_sku
                if str(r.get("line_barcode", "")).strip():
                    line["barcode"] = str(r.get("line_barcode", "")).strip()
                if str(r.get("line_stone_type", "")).strip():
                    line["description"] = str(r.get("line_stone_type", "")).strip()
                wt = _f(r, "line_weight_ct")
                if wt:
                    line["weight"] = wt
                qty = _f(r, "line_qty")
                line["quantity"] = qty if qty is not None else 1.0
                up = _f(r, "line_unit_price")
                if up:
                    line["unit_price"] = up
                if line_total:
                    line["total_price"] = line_total
                cost = _f(r, "line_cost_basis")
                if cost:
                    line["cost_basis"] = cost
                doc_map[key]["line_items"].append(line)

        records: list[dict] = []
        for (doc_type, doc_number), data in doc_map.items():
            idem = f"csv:doc:{doc_type}:{doc_number}".lower()
            records.append({
                "entity_id": f"doc:{uuid.uuid4()}",
                "event_type": "doc.created",
                "data": data,
                "source": "csv_import",
                "idempotency_key": idem,
            })

        try:
            result = await api.batch_import(token, "/docs/import/batch", records, upsert=upsert)
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
            entity_label=t("docs_import.entity_documents"),
            back_href="/docs",
            import_more_href="/docs/import",
            has_mapping=True,
        )
