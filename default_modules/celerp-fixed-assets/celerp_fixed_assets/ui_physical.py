# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The physical side of an asset: its files, its printed tag, the assets a member holds
as custodian, and physical checks by tag with the report of assets not seen since a date."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote, urlencode

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.files import files_section, linked_files
from ui.components.shell import page_header, toast_header
from ui.components.table import EMPTY, breadcrumbs
from ui.config import get_claims, get_role
from ui.config import get_token as _token
from ui.i18n import t

from celerp_fixed_assets.files import ASSET_FILE_TAGS
from celerp_fixed_assets.ui_routes import _error_text, _field, _hx_write, _input, _page, _refused, _text

_CHECKS = "/fixed-assets/checks"


def _files_base(asset_id: str) -> str:
    return f"/fixed-assets/assets/{quote(asset_id, safe=':')}/files"


def _files_card(asset_id: str, listed: dict, **kwargs) -> FT:
    """An asset's own files, then those on the bill or invoice behind it, each on its document."""
    docs: dict[str, dict] = {}
    for f in listed["linked"]:
        docs.setdefault(f["document"], {"entity_id": f["document"], "ref_id": f["document_ref"], "files": []})["files"].append(f)
    return files_section("fa_asset", asset_id, linked_files(listed["files"], list(docs.values())), tags=ASSET_FILE_TAGS,
                         base_url=_files_base(asset_id), **kwargs)


async def _files_fragment(token: str, asset_id: str, **kwargs) -> FT:
    return _files_card(asset_id, await api.fixed_assets_get(token, f"/assets/{quote(asset_id, safe=':')}/files"), **kwargs)


def _tag_card(asset: dict, request: Request, settings: dict) -> FT:
    """The asset number, which can always be written on a tag by hand, and how to print one."""
    from ui.components.shell import module_active
    from ui.routes.modules_page import turn_on_action

    here = f"/fixed-assets/assets/{asset['id']}"
    if module_active(settings, "celerp-labels"):
        action = A(t("fixed_assets.print_tag"), cls="btn btn--secondary",
                   href="/labels/print-source?" + urlencode({"source": "fa_asset", "ids": asset["id"], "back": here}))
    else:
        action = turn_on_action(get_role(request), settings, "celerp-labels", t("fixed_assets.turn_on_labels"),
                                t("fixed_assets.ask_admin_labels"), here)
    return Div(H2(t("fixed_assets.asset_tag"), cls="section-title"),
               P(Strong(asset["number"]), " ", _text(asset.get("tag")) if asset.get("tag") else ""), action,
               cls="detail-card section-card", id="fa-tag")


async def physical_cards(request: Request, token: str, asset: dict) -> list[FT]:
    """The tag and files cards on an asset's page."""
    try:
        settings = (await api.get_company(token)).get("settings") or {}
    except APIError:
        settings = {}
    return [_tag_card(asset, request, settings),
            Div(await _files_fragment(token, asset["id"]), cls="detail-card section-card")]


# ── My assets ────────────────────────────────────────────────────────────────


def _mine_card(asset: dict) -> FT:
    asset_id = quote(asset["id"], safe=":")
    confirmed = asset.get("confirmed") or {}
    files = [A(f["filename"], href=f"/fixed-assets/mine/{asset_id}/files/{f['id']}", target="_blank")
             for f in asset.get("files") or []]
    return Div(
        H2(f"{asset['number']} {asset.get('name') or ''}".strip(), cls="section-title"),
        Table(Tbody(*[Tr(Th(t(label)), Td(_text(value))) for label, value in (
            ("fixed_assets.tag", asset.get("tag")), ("fixed_assets.serial", asset.get("serial")),
            ("fixed_assets.make", asset.get("make")), ("fixed_assets.model", asset.get("model")),
            ("fixed_assets.location", asset.get("location")),
            ("fixed_assets.last_confirmed", confirmed.get("date")))],
            Tr(Th(t("label.files")), Td(*[Div(a) for a in files] or [EMPTY]))), cls="data-table detail-table"),
        Div(Button(t("fixed_assets.confirm_held"), type="button", cls="btn btn--primary",
                   hx_post=f"/fixed-assets/mine/{asset_id}/confirm", hx_swap="none"), cls="form-actions"),
        Form(_field(t("fixed_assets.problem"), _input("text", required=True)),
             _field(t("fixed_assets.problem_photo"), Input(type="file", name="photo", accept="image/*", cls="form-input")),
             Div(Button(t("fixed_assets.report_problem"), type="submit", cls="btn btn--secondary"), cls="form-actions"),
             hx_post=f"/fixed-assets/mine/{asset_id}/problems", hx_encoding="multipart/form-data", hx_swap="none"),
        cls="detail-card section-card")


# ── Checks ───────────────────────────────────────────────────────────────────


def _checks_list(checks: list[dict], me: str) -> FT:
    """Recorded checks, newest first; the person who recorded one may remove it the same day."""
    if not checks:
        return Div(P(t("fixed_assets.no_checks"), cls="form-hint"), id="fa-checks")
    rows = [Tr(Td(c["date"]), Td(A(c["number"], href=f"/fixed-assets/assets/{c['asset_id']}")),
               Td(Button(t("fixed_assets.remove_check"), type="button", cls="btn btn--sm btn--secondary",
                         hx_delete=f"{_CHECKS}/{quote(c['id'], safe=':')}", hx_swap="none")
                  if c["user_id"] == me else ""))
            for c in checks]
    return Div(Table(Thead(Tr(Th(t("fixed_assets.date")), Th(t("fixed_assets.number")), Th(""))), Tbody(*rows),
                     cls="data-table"), id="fa-checks")


def _not_seen_table(items: list[dict]) -> FT:
    if not items:
        return P(t("fixed_assets.all_seen"), cls="form-hint")
    return Table(Thead(Tr(*[Th(t(f"fixed_assets.{k}")) for k in ("number", "tag", "name", "location", "last_seen")])),
                 Tbody(*[Tr(Td(r["number"]), Td(_text(r.get("tag"))), Td(_text(r.get("name"))),
                            Td(_text(r.get("location"))), Td(_text(r.get("last_seen")))) for r in items]),
                 cls="data-table")


def _not_seen_rows(items: list[dict]) -> list[list]:
    return [[t(f"fixed_assets.{k}") for k in ("number", "tag", "name", "location", "last_seen")]] + [
        [r["number"], r.get("tag") or "", r.get("name") or "", r.get("location") or "", r.get("last_seen") or ""]
        for r in items]


def setup_physical_routes(app) -> None:
    """Register the files, custodian and checks pages."""

    # ── Files on an asset ────────────────────────────────────────────────────

    async def _file_write(request: Request, asset_id: str, method: str, suffix: str, data: dict | None = None,
                          files: dict | None = None):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            await api.fixed_assets_form(token, method, f"/assets/{quote(asset_id, safe=':')}/files{suffix}",
                                        data or {}, files=files)
            return await _files_fragment(token, asset_id)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")

    @app.post("/fixed-assets/assets/{asset_id}/files")
    async def asset_file_upload(request: Request, asset_id: str):
        form = await request.form()
        file = form.get("file")
        if not hasattr(file, "read"):
            return P(t("msg.upload_failed"), cls="cell-error")
        payload = {"file": (file.filename or "upload", await file.read(), file.content_type or "application/octet-stream")}
        return await _file_write(request, asset_id, "POST", "", {"document_tag": str(form.get("document_tag") or "")},
                                 files=payload)

    @app.post("/fixed-assets/assets/{asset_id}/files/{file_id}/tag")
    async def asset_file_tag(request: Request, asset_id: str, file_id: str):
        form = await request.form()
        return await _file_write(request, asset_id, "POST", f"/{quote(file_id, safe='')}/tag",
                                 {"document_tag": str(form.get("document_tag") or "")})

    @app.post("/fixed-assets/assets/{asset_id}/files/{file_id}/description")
    async def asset_file_describe(request: Request, asset_id: str, file_id: str):
        form = await request.form()
        return await _file_write(request, asset_id, "PATCH", f"/{quote(file_id, safe='')}/description",
                                 {"description": str(form.get("description") or "")})

    @app.delete("/fixed-assets/assets/{asset_id}/files/{file_id}")
    async def asset_file_delete(request: Request, asset_id: str, file_id: str):
        return await _file_write(request, asset_id, "DELETE", f"/{quote(file_id, safe='')}")

    @app.get("/fixed-assets/assets/{asset_id}/files/_section")
    async def asset_files_section(request: Request, asset_id: str, page: int = 1, sort_dir: str = "desc",
                                  tag_filter: str = "", date_from: str = "", date_to: str = "", search: str = ""):
        token = _token(request)
        if not token:
            return P(t("error.session_expired"), cls="cell-error")
        try:
            return await _files_fragment(token, asset_id, page=page, sort_dir=sort_dir, tag_filter=tag_filter,
                                         date_from=date_from, date_to=date_to, search=search)
        except APIError as e:
            return P(_error_text(e), cls="cell-error")

    async def _download(request: Request, path: str):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            r = await api.fixed_assets_download(token, path)
        except APIError as e:
            return Response(_error_text(e), status_code=e.status)
        return Response(r.content, media_type=r.headers.get("content-type", "application/octet-stream"),
                        headers={k: v for k, v in r.headers.items() if k.lower() == "content-disposition"})

    @app.get("/fixed-assets/assets/{asset_id}/files/{file_id}/download")
    async def asset_file_download(request: Request, asset_id: str, file_id: str):
        return await _download(request, f"/assets/{quote(asset_id, safe=':')}/files/{quote(file_id, safe='')}")

    # ── My assets ────────────────────────────────────────────────────────────

    @app.get("/fixed-assets/mine")
    async def mine_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            items = (await api.fixed_assets_get(token, "/mine"))["items"]
        except APIError as e:
            return await _refused(request, e, "fixed_assets.mine_title")
        body = [_mine_card(a) for a in items] or [P(t("fixed_assets.mine_empty"), cls="form-hint")]
        return await _page(request, page_header(t("fixed_assets.mine_title")),
                           P(t("fixed_assets.mine_hint"), cls="settings-hint"), *body,
                           title_key="fixed_assets.mine_title", nav="my_assets")

    @app.get("/fixed-assets/mine/nav")
    async def mine_nav(request: Request, active: int = 0):
        """The sidebar link, shown only to someone holding an asset."""
        token = _token(request)
        try:
            held = bool(token) and bool((await api.fixed_assets_get(token, "/mine"))["items"])
        except APIError:
            held = False
        if not held:
            return Response("")
        return A(t("nav.my_assets"), href="/fixed-assets/mine", cls=f"nav-link {'nav-link--active' if active else ''}")

    @app.get("/fixed-assets/mine/{asset_id}/files/{file_id}")
    async def mine_file(request: Request, asset_id: str, file_id: str):
        return await _download(request, f"/mine/{quote(asset_id, safe=':')}/files/{quote(file_id, safe='')}")

    @app.post("/fixed-assets/mine/{asset_id}/confirm")
    async def mine_confirm(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        return await _hx_write(token, "POST", f"/mine/{quote(asset_id, safe=':')}/confirm", {}, "/fixed-assets/mine")

    @app.post("/fixed-assets/mine/{asset_id}/problems")
    async def mine_problem(request: Request, asset_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        form = await request.form()
        photo = form.get("photo")
        files = ({"photo": (photo.filename, await photo.read(), photo.content_type or "application/octet-stream")}
                 if hasattr(photo, "read") and photo.filename else None)
        try:
            await api.fixed_assets_form(token, "POST", f"/mine/{quote(asset_id, safe=':')}/problems",
                                        {"text": str(form.get("text") or "")}, files=files)
        except APIError as e:
            return Response(headers=toast_header(_error_text(e), "error", persist=True))
        return Response(headers={"HX-Redirect": "/fixed-assets/mine", **toast_header(t("fixed_assets.problem_sent"))})

    # ── Checks ───────────────────────────────────────────────────────────────

    @app.get(_CHECKS)
    async def checks_page(request: Request, since: str = ""):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        title = "fixed_assets.checks_title"
        try:
            checks = (await api.fixed_assets_get(token, "/checks"))["items"]
            report = await _not_seen(token, since) if since else None
        except APIError as e:
            return await _refused(request, e, title)
        scan = Form(_field(t("fixed_assets.scan_tag"), _input("tag", required=True, autofocus=True, autocomplete="off")),
                    Div(Button(t("fixed_assets.record_check"), type="submit", cls="btn btn--primary"), cls="form-actions"),
                    hx_post=_CHECKS, hx_target="#fa-checks", hx_swap="outerHTML",
                    hx_on__after_request="if(event.detail.successful){this.reset();this.tag.focus();}", cls="flex-row")
        since_form = Form(_field(t("fixed_assets.not_seen_since"),
                                 _input("since", since or date.today().replace(day=1).isoformat(), type_="date", required=True)),
                          Div(Button(t("fixed_assets.show"), type="submit", cls="btn btn--secondary"), cls="form-actions"),
                          method="get", action=_CHECKS, cls="flex-row")
        found: list = []
        if report is not None:
            from ui.components.report_kit import action_bar

            found = [action_bar(f"{_CHECKS}/not-seen/print", f"{_CHECKS}/not-seen.csv", {"since": since}),
                     _not_seen_table(report)]
        crumbs = breadcrumbs([(t("fixed_assets.title"), "/fixed-assets"), (t(title), None)])
        return await _page(request, crumbs, page_header(t(title)), P(t("fixed_assets.checks_hint"), cls="settings-hint"),
                           Div(scan, _checks_list(checks, str(get_claims(request).get("sub", ""))),
                               cls="detail-card section-card"),
                           Div(H2(t("fixed_assets.not_seen_title"), cls="section-title"), since_form, *found,
                               cls="detail-card section-card"), title_key=title)

    @app.post(_CHECKS)
    async def record_check(request: Request):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        tag = str((await request.form()).get("tag") or "")
        try:
            check = await api.fixed_assets_send(token, "POST", "/checks", {"tag": tag})
            checks = (await api.fixed_assets_get(token, "/checks"))["items"]
        except APIError as e:
            return Response(status_code=204, headers=toast_header(_error_text(e), "error", persist=True))
        return Response(to_xml(_checks_list(checks, str(get_claims(request).get("sub", "")))),
                        headers=toast_header(t("fixed_assets.check_recorded", number=check["number"])))

    @app.delete(f"{_CHECKS}/{{check_id}}")
    async def remove_check(request: Request, check_id: str):
        token = _token(request)
        if not token:
            return Response(status_code=401)
        return await _hx_write(token, "DELETE", f"/checks/{quote(check_id, safe=':')}", {}, _CHECKS)

    async def _not_seen(token: str, since: str) -> list[dict]:
        return (await api.fixed_assets_get(token, "/checks/not-seen", {"since": since}))["items"]

    @app.get(f"{_CHECKS}/not-seen.csv")
    async def not_seen_csv(request: Request, since: str = ""):
        from ui.components.report_kit import csv_response, fname_date, plain_error_response

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            items = await _not_seen(token, since)
        except APIError as e:
            return plain_error_response(e)
        return csv_response(_not_seen_rows(items), f"assets-not-seen-since-{fname_date(since)}.csv")

    @app.get(f"{_CHECKS}/not-seen/print")
    async def not_seen_print(request: Request, since: str = ""):
        from ui.components.report_kit import plain_error_response, print_shell

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        try:
            items = await _not_seen(token, since)
            company = await api.get_company(token)
        except APIError as e:
            return plain_error_response(e)
        return print_shell(company, t("fixed_assets.not_seen_title"), t("fixed_assets.not_seen_since_date", date=since),
                           _not_seen_table(items))
