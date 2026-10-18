# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Importing an opening register from a file, and the imports made.

GET  /fixed-assets/import               Upload a register file; the imports made, newest first
GET  /fixed-assets/import/template      The register columns, with an example row
POST /fixed-assets/import/preview       Map the file's columns to the register's
POST /fixed-assets/import/mapped        Save the mapped rows as a draft and open its review
GET  /fixed-assets/import/draft/{ref}   The review: every row's problems, the possible duplicates and
                                        the accounts the import adds to, each against the ledger
GET  /fixed-assets/imports/{id}         One import: its rows, fix and import those left out, undo it
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import page_header, toast_header
from celerp.importers.tabular import known_headers
from ui.components.table import breadcrumbs
from ui.i18n import t
from ui.routes.csv_import import (
    _rows_to_csv,
    apply_column_mapping,
    apply_fixes_to_rows,
    column_mapping_form,
    load_import_draft,
    plan_error_report_response,
    plan_review_panel,
    save_import_draft,
    stage_tabular_upload,
    stash_import_csv,
    upload_form,
    validate_column_mapping,
)

from celerp_fixed_assets.imports import COLUMNS, ROW_STATUSES
from celerp_fixed_assets.ui_routes import (_context, _detail_table, _error_text, _first_period_card, _input, _op_form,
                                           _opening_table, _page, _refused, _send, _start_panel, _text, _token)

_HOME = "/fixed-assets/import"
_TITLE = "fixed_assets.import_title"
_REQUIRED = {"name", "class", "available_date", "cost"}
# Each register column by the label the asset pages give the same field.
_LABEL_KEYS = {
    "number": "number", "name": "name", "class": "class", "description": "description", "serial": "serial",
    "location": "location", "custodian": "custodian", "acquired_date": "acquired_date",
    "available_date": "available_date", "cost": "cost", "residual_percent": "residual_percent",
    "method": "method", "life_months": "life_months", "total_units": "param.total_units",
    "accumulated_depreciation": "accumulated_depreciation", "accumulated_impairment": "accumulated_impairment",
    "revaluation_surplus": "revaluation_surplus", "disposed_date": "disposal_date",
    "disposal_proceeds": "proceeds", "parent_number": "parent_number", "tag": "tag",
}
# The sample row leaves the number blank, so importing it as it stands numbers it in sequence.
_EXAMPLE = {"name": "Warehouse", "class": "Buildings", "available_date": "2020-01-01",
            "cost": "250000", "accumulated_depreciation": "40000"}
_MONEY = ("cost", "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus", "disposal_proceeds")


def _labels() -> dict[str, str]:
    return {col: t(f"fixed_assets.{key}") for col, key in _LABEL_KEYS.items()}


def _headers() -> dict[str, str]:
    """Each header the template writes, by the column it fills."""
    return {label: col for col, label in _labels().items()}


def _batch_href(batch_id: str) -> str:
    return f"/fixed-assets/imports/{quote(batch_id, safe=':')}"


def _upload(cutover: str, error: str | None = None) -> FT:
    return upload_form(cols=list(COLUMNS), template_href=f"{_HOME}/template", preview_action=f"{_HOME}/preview",
                       has_mapping=True, hint=t("fixed_assets.import_hint", cutover=cutover), error=error)


async def _upload_page(request: Request, token: str, error: str | None = None):
    """The upload, once there is a first managed period for the register to open the day before."""
    try:
        setup, currency = await _context(token)
        if not setup["started"]:
            return await _page(request, page_header(t(_TITLE)), _start_panel(currency), title_key=_TITLE)
        cutover = setup["settings"]["cutover"]
        earlier = (await api.fixed_assets_get(token, "/imports"))["items"] if cutover else []
    except APIError as e:
        return await _refused(request, e, _TITLE)
    body = _upload(cutover, error) if cutover else _first_period_card(setup["first_period"], back=_HOME)
    return await _page(request, breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t(_TITLE), None)]),
                       page_header(t(_TITLE)), body, Div(_earlier_card(earlier), cls="mt-md") if earlier else "",
                       title_key=_TITLE)


def _earlier_card(items: list[dict]) -> FT:
    """The imports made, newest first."""
    rows = [Tr(Td(A(_text(b.get("recorded_at"))[:10], href=_batch_href(b["id"]))), Td(_text(b.get("source"))),
               Td(_text(b.get("as_at"))), Td(t(f"fixed_assets.import_state.{b['state']}")),
               *[Td(str((b.get("counts") or {}).get(s, 0)), cls="cell--number") for s in ROW_STATUSES])
            for b in items]
    return _card("fixed_assets.earlier_imports",
                 Table(Thead(Tr(Th(t("fixed_assets.date")), Th(t("fixed_assets.import_source")),
                                Th(t("fixed_assets.as_at")), Th(t("fixed_assets.status")),
                                *[Th(t(f"fixed_assets.import_rows.{s}"), cls="cell--number") for s in ROW_STATUSES])),
                       Tbody(*rows), cls="data-table", id="fa-imports"))


def _mapping(form, csv_text: str, csv_ref: str, errors: list[str] | None = None) -> FT:
    reader = csv.DictReader(io.StringIO(csv_text))
    return column_mapping_form(csv_cols=list(reader.fieldnames or []), target_cols=list(COLUMNS), csv_ref=csv_ref,
                               sample_rows=list(reader), confirm_action=f"{_HOME}/mapped", back_href=_HOME,
                               required_targets=_REQUIRED, errors=errors, form_values=dict(form) if errors else None,
                               col_labels=_labels(), custom_fields=False, aliases=_headers())


def _draft_rows(loaded: tuple[str, dict, int]) -> tuple[list[dict], list[str], dict, int]:
    csv_text, draft, revision = loaded
    reader = csv.DictReader(io.StringIO(csv_text))
    return list(reader), list(reader.fieldnames or []), draft, revision


def _excluded(draft: dict) -> list[int]:
    return [int(n) for n in draft.get("exclude") or []]


def _operation_key(ref: str, rows: list[dict], exclude: list[int]) -> str:
    """One key per draft and its exact rows and exclusions, so the review and the import
    it confirms are the same operation, and a changed draft is a new one."""
    digest = hashlib.sha256(json.dumps([ref, rows, exclude], sort_keys=True).encode()).hexdigest()
    return f"imp-{digest[:40]}"


async def _send_rows(token: str, ref: str, rows: list[dict], draft: dict, **body) -> dict:
    setup, _ = await _context(token)
    exclude = _excluded(draft)
    return await _send(token, "POST", "/imports", {
        "rows": [{k: v for k, v in r.items() if v} for r in rows], "as_at": setup["settings"]["cutover"],
        "source": draft.get("filename") or "", "exclude": exclude, "op_key": _operation_key(ref, rows, exclude),
        **body})


def _duplicates_card(duplicates: list[dict]) -> FT | str:
    if not duplicates:
        return ""
    return Div(P(t("fixed_assets.possible_duplicates"), cls="settings-hint"),
               Ul(*[Li(f"{t('import.col_row')} {d['row']}: {d['message']}") for d in duplicates], cls="list-disc"),
               cls="flash flash--warning")


def _review_extra(plan: dict, currency: str) -> FT:
    counts = plan.get("counts") or {}
    return Div(P(t("fixed_assets.import_rows_as_at", cutover=plan["as_at"]), cls="settings-hint"),
               P(t("fixed_assets.history_count", n=counts["history"]), cls="settings-hint") if counts.get("history")
               else "",
               _duplicates_card(plan.get("duplicates") or []),
               _opening_table(plan.get("accounts") or [], currency))


async def _review(token: str, ref: str, *, notice: str = "") -> FT:
    """The API's preview of the saved draft, rendered as the review."""
    loaded = await load_import_draft(token, ref)
    if loaded is None:
        return P(t("import.csv_expired"), cls="flash flash--error")
    rows, cols, draft, revision = _draft_rows(loaded)
    try:
        plan = await _send_rows(token, ref, rows, draft, preview=True)
        _, currency = await _context(token)
    except APIError as e:
        return Div(P(_error_text(e), cls="flash flash--error"),
                   A(t("import.upload_corrected_file"), href=_HOME, cls="btn btn--secondary"), id="import-preview")
    return plan_review_panel(
        rows=rows, cols=cols, col_labels=_labels(), csv_ref=ref, revision=revision,
        plan={**plan, "preview_hash": plan["fingerprint"], "decisions": {"exclude": _excluded(draft)}},
        review_action=f"{_HOME}/review", confirm_action=f"{_HOME}/confirm", error_report_action=f"{_HOME}/errors",
        cancel_action=f"{_HOME}/cancel", upload_href=_HOME, notice=notice, extra=_review_extra(plan, currency),
        money_cols=_MONEY, currency=currency)


def _card(title_key: str, *body) -> FT:
    return Div(H2(t(title_key), cls="section-title"), *body, cls="detail-card section-card")


def _batch_details(batch: dict) -> FT:
    counts = batch.get("counts") or {}
    return _detail_table([
        (t("fixed_assets.as_at"), _text(batch.get("as_at"))),
        (t("fixed_assets.import_source"), _text(batch.get("source"))),
        (t("fixed_assets.status"), t(f"fixed_assets.import_state.{batch['state']}")),
        (t("fixed_assets.date"), _text(batch.get("recorded_at"))[:10]),
        *[(t(f"fixed_assets.import_rows.{s}"), str(counts.get(s, 0))) for s in ROW_STATUSES]])


def _imported_table(rows: list[dict]) -> FT:
    body = [Tr(Td(str(r["row"]), cls="cell--number"),
               Td(A(r["number"], href=f"/fixed-assets/assets/{quote(r['asset_id'], safe=':')}")),
               Td(_text(r["values"].get("name"))), Td(_text(r["values"].get("class"))))
            for r in rows]
    return Table(Thead(Tr(Th(t("import.col_row"), cls="cell--number"), Th(t("fixed_assets.number")),
                          Th(t("fixed_assets.name")), Th(t("fixed_assets.class")))),
                 Tbody(*body), cls="data-table", id="fa-import-imported")


def _history_table(rows: list[dict]) -> FT:
    body = [Tr(Td(str(r["row"]), cls="cell--number"), Td(_text(r["values"].get("number"))),
               Td(_text(r["values"].get("name"))), Td(_text(r["values"].get("disposed_date"))))
            for r in rows]
    return Table(Thead(Tr(Th(t("import.col_row"), cls="cell--number"), Th(t("fixed_assets.number")),
                          Th(t("fixed_assets.name")), Th(t("fixed_assets.disposal_date")))),
                 Tbody(*body), cls="data-table", id="fa-import-history")


def _problem(errors: list[dict], column: str) -> str:
    return "; ".join(e["message"] for e in errors if e["field"] == column)


def _rejected_form(batch_id: str, rows: list[dict], labels: dict[str, str]) -> FT:
    """Each row left out, its values open to correction; the ticked rows are imported into this import."""
    shown = [c for c in COLUMNS if any(c in r["values"] or any(e["field"] == c for e in r["errors"]) for r in rows)]
    body = []
    for r in rows:
        n = r["row"]
        bad = {e["field"] for e in r["errors"]}
        body.append(Tr(
            Td(Input(type="checkbox", name="row", value=str(n), checked=True, aria_label=f"{t('import.col_row')} {n}")),
            Td(str(n), cls="cell--number"),
            *[Td(_input(f"v.{n}.{c}", r["values"].get(c, ""), aria_label=f"{labels[c]} {n}"),
                 P(_problem(r["errors"], c), cls="cell-error") if c in bad else "") for c in shown],
            Td(t("fixed_assets.import_fix_cells") if bad else t("fixed_assets.import_left_out"))))
    return _op_form(
        "import-rows",
        Input(type="hidden", name="batch_id", value=batch_id),
        P(t("fixed_assets.fix_rows_hint"), cls="settings-hint"),
        Div(Table(Thead(Tr(Th(t("fixed_assets.import_take")), Th(t("import.col_row"), cls="cell--number"),
                           *[Th(labels[c]) for c in shown], Th(t("import.col_problem")))),
                  Tbody(*body), cls="data-table"), cls="table-scroll-wrap"),
        review_label=t("fixed_assets.fix_and_import"))


def setup_import_routes(app) -> None:
    """Register the register import pages into the FastHTML app."""

    @app.get(_HOME)
    async def import_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await _upload_page(request, token)

    @app.get(f"{_HOME}/template")
    async def import_template(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        out = io.StringIO()
        labels = _labels()
        csv.writer(out, lineterminator="\n").writerows([[labels[c] for c in COLUMNS],
                                                          [_EXAMPLE.get(c, "") for c in COLUMNS]])
        return PlainTextResponse(out.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=asset_register.csv"})

    @app.post(f"{_HOME}/preview")
    async def import_preview(request: Request):
        """The uploaded file's columns, each to be mapped to a register column."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        rows, ref, err = await stage_tabular_upload(token, form, known=known_headers([*COLUMNS, *_headers()]))
        if err:
            return await _upload_page(request, token, err)
        loaded = await load_import_draft(token, ref)
        if loaded is None:
            return await _upload_page(request, token, t("import.csv_expired"))
        return await _page(request, page_header(t(_TITLE)), _mapping(form, loaded[0], ref), title_key=_TITLE)

    @app.post(f"{_HOME}/mapped")
    async def import_mapped(request: Request):
        """The mapped rows become the import's draft; every later step reads it."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        form = await request.form()
        ref = str(form.get("csv_ref") or "")
        loaded = await load_import_draft(token, ref)
        if loaded is None:
            return await _upload_page(request, token, t("import.csv_expired"))
        csv_text, uploaded, _ = loaded
        original = list(csv.DictReader(io.StringIO(csv_text)).fieldnames or [])
        errors = validate_column_mapping(form, original, core_fields=set(COLUMNS), required_targets=_REQUIRED)
        if not errors:
            mapped_csv, mapped_cols = apply_column_mapping(form, csv_text)
            errors = [t("fixed_assets.import_unknown_column", col=c) for c in mapped_cols if c not in COLUMNS]
        if errors:
            return await _page(request, page_header(t(_TITLE)), _mapping(form, csv_text, ref, errors),
                               title_key=_TITLE)
        draft = {"filename": (uploaded.get("source") or {}).get("filename") or "", "exclude": []}
        draft_ref = await stash_import_csv(token, mapped_csv, draft)
        return RedirectResponse(f"{_HOME}/draft/{draft_ref}", status_code=303)

    @app.get(f"{_HOME}/draft/{{ref}}")
    async def import_draft(request: Request, ref: str):
        """The current review of a draft, so a refresh, Back or a language change keeps it."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        return await _page(request, breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t(_TITLE), _HOME),
                                                 (t("fixed_assets.review"), None)]),
                           page_header(t(_TITLE)), await _review(token, ref), title_key=_TITLE)

    @app.post(f"{_HOME}/review")
    async def import_review(request: Request):
        """Save the cell fixes and the rows left out at the reviewed revision; review again."""
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        ref = str(form.get("csv_ref") or "")
        loaded = await load_import_draft(token, ref)
        if loaded is None:
            return P(t("import.csv_expired"), cls="flash flash--error")
        rows, cols, draft, _ = _draft_rows(loaded)
        rows = apply_fixes_to_rows(form, rows, cols)
        exclude = sorted({int(v) for v in form.getlist("exclude") if str(v).isdigit()})
        try:
            submitted = int(str(form.get("revision") or ""))
        except ValueError:
            submitted = -1
        saved = await save_import_draft(token, ref, _rows_to_csv(rows, cols), {**draft, "exclude": exclude},
                                        submitted)
        return await _review(token, ref, notice="" if saved is not None else t("fixed_assets.import_draft_changed"))

    @app.post(f"{_HOME}/errors")
    async def import_errors(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        ref = str((await request.form()).get("csv_ref") or "")
        loaded = await load_import_draft(token, ref)
        if loaded is None:
            return await _upload_page(request, token, t("import.csv_expired"))
        rows, cols, draft, _ = _draft_rows(loaded)
        try:
            plan = await _send_rows(token, ref, rows, draft, preview=True)
        except APIError as e:
            return await _upload_page(request, token, _error_text(e))
        return plan_error_report_response(rows, cols, plan["errors"], "asset_register_errors.csv")

    @app.post(f"{_HOME}/cancel")
    async def import_cancel(request: Request):
        """Discard the draft and go back to the upload."""
        from celerp.services import import_stage

        token = _token(request)
        if not token:
            return Response(status_code=401)
        ref = str((await request.form()).get("csv_ref") or "")
        if await load_import_draft(token, ref) is not None:
            import_stage.delete_ref(ref)
        return Response("", headers={"HX-Redirect": _HOME})

    @app.post(f"{_HOME}/confirm")
    async def import_confirm(request: Request):
        """Import exactly the reviewed draft: the API refuses when its preview no longer matches."""
        from celerp.services import import_stage

        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        ref = str(form.get("csv_ref") or "")
        loaded = await load_import_draft(token, ref)
        if loaded is None:
            return P(t("import.csv_expired"), cls="flash flash--error")
        rows, _, draft, _ = _draft_rows(loaded)
        try:
            result = await _send_rows(token, ref, rows, draft, fingerprint=str(form.get("preview_hash") or ""))
        except APIError as e:
            return await _review(token, ref, notice=_error_text(e))
        import_stage.delete_ref(ref)
        return Response("", headers={"HX-Redirect": _batch_href(result["batch"]["id"]),
                                     **toast_header(t("fixed_assets.toast.imported"))})

    @app.get("/fixed-assets/imports/{batch_id}")
    async def import_batch_page(request: Request, batch_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        path = f"/imports/{quote(batch_id, safe=':')}"
        try:
            batch = await api.fixed_assets_get(token, path, {"limit": 500})
            _, currency = await _context(token)
        except APIError as e:
            return await _refused(request, e, _TITLE)
        by_status = {s: [r for r in batch["rows"] if r["status"] == s] for s in ROW_STATUSES}
        live = batch["state"] == "imported"
        cards = [_card("fixed_assets.details", _batch_details(batch)),
                 _card("fixed_assets.opening_title", _opening_table(batch["accounts"], currency))]
        if by_status["imported"]:
            cards.append(_card("fixed_assets.import_rows.imported", _imported_table(by_status["imported"])))
        if by_status["rejected"]:
            cards.append(_card("fixed_assets.import_rows.rejected",
                               _rejected_form(batch_id, by_status["rejected"], _labels()) if live
                               else _history_table(by_status["rejected"])))
        if by_status["history"]:
            cards.append(_card("fixed_assets.import_rows.history", _history_table(by_status["history"])))
        if batch["total"] > len(batch["rows"]):
            cards.append(P(t("import.showing_rows", n=len(batch["rows"]), total=batch["total"]), cls="form-hint"))
        if live:
            cards.append(_card("fixed_assets.undo_import",
                               _op_form("undo-import", Input(type="hidden", name="batch_id", value=batch_id),
                                        P(t("fixed_assets.undo_import_hint"), cls="settings-hint"),
                                        review_label=t("fixed_assets.undo_import"))))
        title = f"{t(_TITLE)}: {_text(batch.get('source'))}"
        return await _page(request, breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t(_TITLE), _HOME),
                                                 (_text(batch.get("source")), None)]),
                           page_header(title), *cards, title_key=_TITLE)
