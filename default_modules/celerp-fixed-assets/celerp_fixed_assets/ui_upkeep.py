# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Maintenance, warranty and insurance on an asset's page: its reminders, each edited in
place, marked done, postponed with a reason, removed and undone with its history shown;
the maintenance log with its files; and the list of reminders due across the company."""
from __future__ import annotations

import json
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.shell import page_header, toast_header
from ui.components.table import breadcrumbs, display_cell, editable_cell, empty_state_cta
from ui.config import get_token as _token
from ui.i18n import t

from celerp_fixed_assets.operations import RESPONSIBLE
from celerp_fixed_assets.upkeep import KINDS, MAINT_EDITABLE, REMINDERS_URL
from celerp_fixed_assets.ui_routes import (
    _STACK, _context, _error_text, _field, _hx_write, _input, _members, _money_td, _money_th, _page, _picker, _refused,
    _text)

# The reminder fields edited in place, with the editor each one uses.
_CELLS = {"due": "date", "every_months": "number", "lead_days": "number", RESPONSIBLE: "select", "policy": "text",
          "insured_value": "money", **dict.fromkeys(KINDS.values(), "text")}
_MAINT_CELLS = {"date": "date", "work": "text", "by": "text"}


def _rid(reminder_id: str) -> str:
    return quote(reminder_id, safe=":")


def _reminder_cell(reminder: dict, field: str, members: dict[str, str]) -> FT:
    url = f"/fixed-assets/reminders/{_rid(reminder['id'])}/field/{field}/edit"
    if reminder["status"] == "removed":
        value = members.get(reminder.get(field)) if field == RESPONSIBLE else reminder.get(field)
        return Td(_text(value))
    if field == RESPONSIBLE:
        return display_cell(reminder["id"], field, reminder.get(field), "select", label_map=members, edit_url=url)
    return display_cell(reminder["id"], field, reminder.get(field), "text", edit_url=url)


def _maint_cell(entry: dict, field: str) -> FT:
    if entry["status"] != "active" or (entry.get("source") and field == "date"):
        return Td(_text(entry.get(field)))
    url = f"/fixed-assets/maintenance/{_rid(entry['id'])}/field/{field}/edit"
    return display_cell(entry["id"], field, entry.get(field), "text", edit_url=url)


def _change_text(row: dict, members: dict[str, str]) -> str:
    """What a history row changed, field by field: before, then after."""
    def shown(field, value):
        if field == "status":
            return t(f"fixed_assets.reminder_status.{value}")
        return members.get(value, value) if field == RESPONSIBLE else _text(value)

    return "; ".join(f"{t(f'fixed_assets.{f}')}: {shown(f, row['before'].get(f))} > {shown(f, v)}"
                     for f, v in row.get("after", {}).items())


def _history(reminder: dict, members: dict[str, str]) -> FT:
    rows = [Li(f"{h['at'][:10]} ", Strong(t(f"fixed_assets.history.{h['action']}")),
               f" {members.get(h['user_id'], '')} ".rstrip() + " ",
               f"{t('fixed_assets.done_on')} {h['date']} " if h.get("date") else "",
               _change_text(h, members), f" ({h['reason']})" if h.get("reason") else "",
               Span(t("fixed_assets.history_undone"), cls="badge badge--muted") if h.get("undone") else "")
            for h in reminder.get("history") or []]
    return Details(Summary(t("fixed_assets.history_title")), Ul(*rows), cls="fa-history")


def _reminder_actions(reminder: dict, today: str) -> FT:
    base = f"/fixed-assets/reminders/{_rid(reminder['id'])}"
    undoable = any(h["action"] not in ("created", "undone") and not h.get("undone")
                   for h in reminder.get("history") or [])
    undo = (Button(t("fixed_assets.undo_change"), type="button", cls="btn btn--sm btn--secondary",
                   hx_post=f"{base}/undo", hx_swap="none") if undoable else "")
    if reminder["status"] != "open":
        return Div(undo, cls="form-actions")
    return Div(
        Div(Button(t("fixed_assets.mark_done"), type="button", cls="btn btn--sm btn--primary", hx_post=f"{base}/done",
                   hx_vals=json.dumps({"date": today}), hx_swap="none"),
            undo, Button(t("fixed_assets.remove_reminder"), type="button", cls="btn btn--sm btn--secondary",
                         hx_post=f"{base}/remove", hx_swap="none"), cls="form-actions"),
        Details(Summary(t("fixed_assets.done_on")),
                Form(_input("date", today, type_="date", aria_label=t("fixed_assets.done_on")),
                     Button(t("fixed_assets.mark_done"), type="submit", cls="btn btn--sm btn--primary"),
                     hx_post=f"{base}/done", hx_swap="none", cls="inline-form")),
        Details(Summary(t("fixed_assets.postpone")),
                Form(_input("to", type_="date", aria_label=t("fixed_assets.postpone_to")),
                     _input("reason", placeholder=t("fixed_assets.reason"), aria_label=t("fixed_assets.reason")),
                     Button(t("fixed_assets.postpone"), type="submit", cls="btn btn--sm btn--secondary"),
                     hx_post=f"{base}/postpone", hx_swap="none", cls="inline-form")))


def _reminders_table(reminders: list[dict], members: dict[str, str], today: str) -> FT:
    if not reminders:
        return P(t("fixed_assets.no_reminders"), cls="form-hint")
    insured = any(r["kind"] == "insurance" for r in reminders)
    extra = ("policy", "insured_value") if insured else ()

    def insurance_cells(r):
        if not insured:
            return []
        if r["kind"] != "insurance":
            return [Td(_text(None)), Td(_text(None), cls="cell--number")]
        return [_reminder_cell(r, "policy", members), _reminder_cell(r, "insured_value", members)]

    head = [Th(t("fixed_assets.reminder_kind")), Th(t("fixed_assets.reminder_detail")), Th(t("fixed_assets.due")),
            Th(t("fixed_assets.every_months"), cls="cell--number"), Th(t("fixed_assets.lead_days"), cls="cell--number"),
            Th(t(f"fixed_assets.{RESPONSIBLE}")), *[Th(t(f"fixed_assets.{f}")) for f in extra[:1]],
            *[_money_th(t(f"fixed_assets.{f}")) for f in extra[1:]], Th(t("fixed_assets.status")), Th("")]
    rows = []
    for r in reminders:
        rows.append(Tr(Td(t(f"fixed_assets.reminder_kind.{r['kind']}")), _reminder_cell(r, KINDS[r["kind"]], members),
                       _reminder_cell(r, "due", members), _reminder_cell(r, "every_months", members),
                       _reminder_cell(r, "lead_days", members), _reminder_cell(r, RESPONSIBLE, members),
                       *insurance_cells(r), Td(t(f"fixed_assets.reminder_status.{r['status']}")),
                       Td(_reminder_actions(r, today), _history(r, members))))
    return Div(Table(Thead(Tr(*head)), Tbody(*rows), cls="data-table", id="fa-reminders"), cls="table-scroll-wrap")


def _add_reminder_form(asset_id: str, members: dict[str, str]) -> FT:
    kinds = [(k, t(f"fixed_assets.reminder_kind.{k}")) for k in KINDS]
    return Details(
        Summary(t("fixed_assets.add_reminder")),
        Form(_field(t("fixed_assets.reminder_kind"), Select(
                 *[Option(text, value=k, selected=k == "maintenance") for k, text in kinds], name="kind",
                 cls="form-input", aria_label=t("fixed_assets.reminder_kind"))),
             _field(t("fixed_assets.reminder_detail"), _input("detail", required=True)),
             _field(t("fixed_assets.due"), _input("due", type_="date", required=True)),
             _field(t("fixed_assets.every_months"), _input("every_months", type_="number", min="1", step="1"),
                    t("fixed_assets.every_months_hint")),
             _field(t("fixed_assets.lead_days"), _input("lead_days", type_="number", min="0", step="1",
                                                        placeholder="30")),
             _field(t(f"fixed_assets.{RESPONSIBLE}"), _picker(RESPONSIBLE, list(members.items()),
                                                              label=t(f"fixed_assets.{RESPONSIBLE}")),
                    t("fixed_assets.reminder_responsible_hint")),
             _field(t("fixed_assets.policy"), _input("policy"), t("fixed_assets.insurance_only_hint")),
             _field(t("fixed_assets.insured_value"), _input("insured_value", type_="number", step="any", min="0"),
                    t("fixed_assets.insurance_only_hint")),
             Div(Button(t("fixed_assets.add_reminder"), type="submit", cls="btn btn--primary"), cls="form-actions"),
             hx_post=f"/fixed-assets/assets/{_rid(asset_id)}/reminders", hx_swap="none", cls=_STACK),
        cls="fa-add write-only")


def _files_cell(asset_id: str, entry: dict) -> FT:
    links = [Div(A(f["filename"], href=f"/fixed-assets/assets/{_rid(asset_id)}/files/{quote(f['id'], safe='')}/download",
                   target="_blank")) for f in entry.get("files") or []]
    attach = (Details(Summary(t("fixed_assets.attach_file")),
                      Form(Input(type="file", name="file", cls="form-input", aria_label=t("fixed_assets.attach_file")),
                           Button(t("fixed_assets.attach_file"), type="submit", cls="btn btn--sm btn--secondary"),
                           hx_post=f"/fixed-assets/maintenance/{_rid(entry['id'])}/files",
                           hx_encoding="multipart/form-data", hx_swap="none", cls="inline-form"))
              if entry["status"] == "active" else "")
    return Td(*links, attach)


def _maintenance_table(asset_id: str, log: list[dict], currency: str) -> FT:
    if not log:
        return P(t("fixed_assets.no_maintenance"), cls="form-hint")

    def action(m):
        if m.get("source"):
            return Span(t("fixed_assets.from_bill"), cls="badge badge--muted")
        verb = "remove" if m["status"] == "active" else "restore"
        return Button(t(f"fixed_assets.{verb}_entry"), type="button", cls="btn btn--sm btn--secondary",
                      hx_post=f"/fixed-assets/maintenance/{_rid(m['id'])}/{verb}", hx_swap="none")

    rows = [Tr(_maint_cell(m, "date"), _maint_cell(m, "work"), _maint_cell(m, "by"), Td(_text(m.get("next_due"))),
               _money_td(m.get("amount"), currency), Td(_text(m.get("bill_ref"))), _files_cell(asset_id, m),
               Td(t(f"fixed_assets.entry_status.{m['status']}")), Td(action(m)))
            for m in log]
    return Div(Table(Thead(Tr(Th(t("fixed_assets.date")), Th(t("fixed_assets.work")), Th(t("fixed_assets.by")),
                              Th(t("fixed_assets.next_due")), _money_th(t("fixed_assets.amount")),
                              Th(t("fixed_assets.bill")), Th(t("label.files")), Th(t("fixed_assets.status")), Th(""))),
                     Tbody(*rows), cls="data-table", id="fa-maintenance"), cls="table-scroll-wrap")


def _add_entry_form(asset_id: str, today: str) -> FT:
    return Details(
        Summary(t("fixed_assets.add_entry")),
        Form(_field(t("fixed_assets.date"), _input("date", today, type_="date", required=True)),
             _field(t("fixed_assets.work"), _input("work", required=True)),
             _field(t("fixed_assets.by"), _input("by")),
             _field(t("fixed_assets.next_due"), _input("next_due", type_="date"), t("fixed_assets.next_due_hint")),
             _field(t("fixed_assets.every_months"), _input("every_months", type_="number", min="1", step="1"),
                    t("fixed_assets.every_months_hint")),
             Div(Button(t("fixed_assets.add_entry"), type="submit", cls="btn btn--primary"), cls="form-actions"),
             hx_post=f"/fixed-assets/assets/{_rid(asset_id)}/maintenance", hx_swap="none", cls=_STACK),
        cls="fa-add write-only")


async def upkeep_card(token: str, asset: dict, members: dict[str, str], currency: str, today: str) -> FT:
    """The reminders and the maintenance log on an asset's page."""
    upkeep = await api.fixed_assets_get(token, f"/assets/{_rid(asset['id'])}/upkeep")
    return Div(H2(t("fixed_assets.upkeep_title"), cls="section-title"),
               P(t("fixed_assets.upkeep_hint"), cls="settings-hint"),
               _reminders_table(upkeep["reminders"], members, today), _add_reminder_form(asset["id"], members),
               H4(t("fixed_assets.maintenance_log")),
               _maintenance_table(asset["id"], upkeep["maintenance"], currency), _add_entry_form(asset["id"], today),
               cls="detail-card section-card", id="fa-upkeep")


def due_soon_card(items: list[dict]) -> FT:
    return Div(H3(t("fixed_assets.due_soon_title")),
               P(t("fixed_assets.due_soon_count", count=len(items)), " ",
                 A(t("fixed_assets.due_soon_open"), href=REMINDERS_URL)),
               cls="flash flash--warning", id="fa-due-soon")


def _form_text(form, name: str) -> str:
    return str(form.get(name) or "").strip()


def setup_upkeep_routes(app) -> None:
    """Register the reminder and maintenance log writes, their cells, and the due list."""

    def _back(asset_id: str) -> str:
        return f"/fixed-assets/assets/{asset_id}"

    async def _asset_of(token: str, path: str) -> str:
        return (await api.fixed_assets_get(token, path))["asset_id"]

    @app.post("/fixed-assets/assets/{asset_id}/reminders")
    async def add_reminder(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        kind = _form_text(form, "kind")
        body = {k: _form_text(form, k) for k in ("kind", "due", "every_months", "lead_days", RESPONSIBLE)}
        body[KINDS.get(kind, "work")] = _form_text(form, "detail")
        if kind == "insurance":
            body.update({k: _form_text(form, k) for k in ("policy", "insured_value")})
        return await _hx_write(token, "POST", f"/assets/{_rid(asset_id)}/reminders", body, _back(asset_id),
                               t("fixed_assets.toast.reminder_added"))

    async def _reminder_write(request: Request, reminder_id: str, action: str, fields: tuple[str, ...]):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        try:
            asset_id = await _asset_of(token, f"/reminders/{_rid(reminder_id)}")
        except APIError as e:
            return Response(headers=_toast_error(e))
        return await _hx_write(token, "POST", f"/reminders/{_rid(reminder_id)}/{action}",
                               {k: _form_text(form, k) for k in fields}, _back(asset_id),
                               t(f"fixed_assets.toast.reminder_{action}"))

    @app.post("/fixed-assets/reminders/{reminder_id}/done")
    async def reminder_done(request: Request, reminder_id: str):
        return await _reminder_write(request, reminder_id, "done", ("date",))

    @app.post("/fixed-assets/reminders/{reminder_id}/postpone")
    async def reminder_postpone(request: Request, reminder_id: str):
        return await _reminder_write(request, reminder_id, "postpone", ("to", "reason"))

    @app.post("/fixed-assets/reminders/{reminder_id}/remove")
    async def reminder_remove(request: Request, reminder_id: str):
        return await _reminder_write(request, reminder_id, "remove", ())

    @app.post("/fixed-assets/reminders/{reminder_id}/undo")
    async def reminder_undo(request: Request, reminder_id: str):
        return await _reminder_write(request, reminder_id, "undo", ())

    async def _reminder_display(token: str, reminder_id: str, field: str) -> FT:
        reminder = await api.fixed_assets_get(token, f"/reminders/{_rid(reminder_id)}")
        return _reminder_cell(reminder, field, await _members(token) if field == RESPONSIBLE else {})

    @app.get("/fixed-assets/reminders/{reminder_id}/field/{field}/edit")
    async def reminder_field_edit(request: Request, reminder_id: str, field: str):
        token = _token(request)
        if not token or field not in _CELLS:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            reminder = await api.fixed_assets_get(token, f"/reminders/{_rid(reminder_id)}")
            members = await _members(token) if field == RESPONSIBLE else None
        except APIError as e:
            return P(_error_text(e), cls="cell-error")
        url = f"/fixed-assets/reminders/{_rid(reminder_id)}/field/{field}"
        choice = ({"cell_type": "select", "options": list(members), "label_map": members, "searchable": True}
                  if members is not None else {"cell_type": _CELLS[field]})
        return editable_cell(reminder_id, field, reminder.get(field), patch_url=url, restore_url=f"{url}/display",
                             aria_label=t(f"fixed_assets.{field}"), **choice)

    @app.get("/fixed-assets/reminders/{reminder_id}/field/{field}/display")
    async def reminder_field_display(request: Request, reminder_id: str, field: str):
        token = _token(request)
        if not token or field not in _CELLS:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            return await _reminder_display(token, reminder_id, field)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")

    @app.patch("/fixed-assets/reminders/{reminder_id}/field/{field}")
    async def reminder_field_patch(request: Request, reminder_id: str, field: str):
        token = _token(request)
        if not token or field not in _CELLS:
            return P(t("fixed_assets.error"), cls="cell-error")
        form = await request.form()
        try:
            reminder = await api.fixed_assets_send(token, "PATCH", f"/reminders/{_rid(reminder_id)}",
                                                   {"changes": {field: str(form.get("value", "")).strip() or None}})
            members = await _members(token) if field == RESPONSIBLE else {}
        except APIError as e:
            return Td(P(_error_text(e), cls="cell-error"))
        return _reminder_cell(reminder, field, members)

    # ── Maintenance log ──────────────────────────────────────────────────────

    @app.post("/fixed-assets/assets/{asset_id}/maintenance")
    async def add_entry(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        return await _hx_write(token, "POST", f"/assets/{_rid(asset_id)}/maintenance",
                               {k: _form_text(form, k) for k in ("date", "work", "by", "next_due", "every_months")},
                               _back(asset_id), t("fixed_assets.toast.entry_added"))

    async def _entry_write(request: Request, maint_id: str, action: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        try:
            asset_id = await _asset_of(token, f"/maintenance/{_rid(maint_id)}")
        except APIError as e:
            return Response(headers=_toast_error(e))
        return await _hx_write(token, "POST", f"/maintenance/{_rid(maint_id)}/{action}", {}, _back(asset_id),
                               t(f"fixed_assets.toast.entry_{action}"))

    @app.post("/fixed-assets/maintenance/{maint_id}/remove")
    async def entry_remove(request: Request, maint_id: str):
        return await _entry_write(request, maint_id, "remove")

    @app.post("/fixed-assets/maintenance/{maint_id}/restore")
    async def entry_restore(request: Request, maint_id: str):
        return await _entry_write(request, maint_id, "restore")

    @app.post("/fixed-assets/maintenance/{maint_id}/files")
    async def entry_file(request: Request, maint_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        file = form.get("file")
        if not hasattr(file, "read"):
            return Response(headers=toast_header(t("msg.upload_failed"), "error", persist=True))
        payload = {"file": (file.filename or "upload", await file.read(), file.content_type or "application/octet-stream")}
        try:
            entry = await api.fixed_assets_form(token, "POST", f"/maintenance/{_rid(maint_id)}/files", {}, files=payload)
        except APIError as e:
            return Response(headers=_toast_error(e))
        return Response(headers={"HX-Redirect": _back(entry["asset_id"]), **toast_header(t("fixed_assets.toast.file_attached"))})

    @app.get("/fixed-assets/maintenance/{maint_id}/field/{field}/edit")
    async def entry_field_edit(request: Request, maint_id: str, field: str):
        token = _token(request)
        if not token or field not in MAINT_EDITABLE:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            entry = await api.fixed_assets_get(token, f"/maintenance/{_rid(maint_id)}")
        except APIError as e:
            return P(_error_text(e), cls="cell-error")
        url = f"/fixed-assets/maintenance/{_rid(maint_id)}/field/{field}"
        return editable_cell(maint_id, field, entry.get(field), _MAINT_CELLS[field], patch_url=url,
                             restore_url=f"{url}/display", aria_label=t(f"fixed_assets.{field}"))

    @app.get("/fixed-assets/maintenance/{maint_id}/field/{field}/display")
    async def entry_field_display(request: Request, maint_id: str, field: str):
        token = _token(request)
        if not token or field not in MAINT_EDITABLE:
            return P(t("fixed_assets.error"), cls="cell-error")
        try:
            return _maint_cell(await api.fixed_assets_get(token, f"/maintenance/{_rid(maint_id)}"), field)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")

    @app.patch("/fixed-assets/maintenance/{maint_id}/field/{field}")
    async def entry_field_patch(request: Request, maint_id: str, field: str):
        token = _token(request)
        if not token or field not in MAINT_EDITABLE:
            return P(t("fixed_assets.error"), cls="cell-error")
        form = await request.form()
        try:
            entry = await api.fixed_assets_send(token, "PATCH", f"/maintenance/{_rid(maint_id)}",
                                                {"changes": {field: str(form.get("value", "")).strip() or None}})
        except APIError as e:
            return Td(P(_error_text(e), cls="cell-error"))
        return _maint_cell(entry, field)

    # ── Due across the company ───────────────────────────────────────────────

    @app.get(REMINDERS_URL)
    async def reminders_page(request: Request):
        """Reminders due or within their lead time. Soonest first, not newest: this is a list
        of work to do, and the most overdue is the most urgent."""
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            await _context(token)
            items = (await api.fixed_assets_get(token, "/reminders"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.due_soon_title")
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t("fixed_assets.due_soon_title"), None)])
        rows = [Tr(Td(A(r["number"], href=_back(r["asset_id"]))), Td(_text(r.get("name"))),
                   Td(t(f"fixed_assets.reminder_kind.{r['kind']}")), Td(_text(r["detail"])), Td(r["due"]),
                   Td(Span(t("fixed_assets.overdue"), cls="badge badge--overdue") if r["overdue"] else ""))
                for r in items]
        body = (Table(Thead(Tr(Th(t("fixed_assets.number")), Th(t("fixed_assets.name")),
                               Th(t("fixed_assets.reminder_kind")), Th(t("fixed_assets.reminder_detail")),
                               Th(t("fixed_assets.due")), Th(""))), Tbody(*rows), cls="data-table") if rows
                else empty_state_cta(t("fixed_assets.reminders_empty"), t("fixed_assets.title"), "/fixed-assets"))
        return await _page(request, crumbs, page_header(t("fixed_assets.due_soon_title")),
                           P(t("fixed_assets.due_soon_hint"), cls="settings-hint"), body,
                           title_key="fixed_assets.due_soon_title")


def _toast_error(e: APIError) -> dict:
    return toast_header(_error_text(e), "error", persist=True)
