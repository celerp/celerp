# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The help viewer: search and browse at /help, one page at /help/{page_id}, its
screenshots, and the "Help for this page" panel the top bar loads on every page.

Search and page reads go through the API, so the viewer and Celerp AI see the same
results and flags. Browsing, related-page titles and screenshots read the help index
in this process, which loads the same modules as the API.
"""
from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

import ui.api_client as api
from celerp.help.registry import CORE, get_index
from celerp.help.schema import HelpPage
from celerp.modules.loader import module_label
from ui.api_client import APIError
from ui.components.shell import base_shell, load_error, page_header
from ui.config import get_role
from ui.config import get_token as _token
from ui.i18n import get_lang, t

_LOGIN = "/login"


def _page_link(page: dict) -> FT:
    return Li(A(page["title"], href=f"/help/{page['id']}", cls="help-list__title"),
              P(page["summary"], cls="help-list__summary"))


def _back(lang: str) -> FT:
    return A(t("acct.post_again_back", lang, page=t("help.title", lang)), href="/help", cls="help-back")


def _ask_ai(lang: str) -> FT:
    return A(t("help.ask_ai", lang), href="/ai", cls="btn btn--secondary btn--sm")


def _search_form(q: str, module: str, lang: str) -> FT:
    return Form(
        Input(type="search", name="q", value=q, placeholder=t("help.search_placeholder", lang),
              aria_label=t("help.search_placeholder", lang), maxlength="200", cls="help-search__input"),
        *([Input(type="hidden", name="module", value=module)] if module else []),
        Button(t("label.search", lang), type="submit", cls="btn btn--primary btn--sm"),
        method="get", action="/help", cls="help-search",
    )


def _module_off(module: str, role: str, lang: str) -> FT:
    """The module-off notice and its way on, as every module page shows it."""
    from ui.routes.modules_page import manages_modules
    way_on = (A(t("nav.modules", lang), href="/modules", cls="btn btn--primary btn--sm")
              if manages_modules(role) else P(t("modules.ask_admin_to_turn_on", lang)))
    return Div(P(t("modules.off_for_company", lang, module=module_label(module)), cls="flash flash--error"),
               way_on)


def _flags(hit: dict, role: str, lang: str) -> list[FT]:
    notes: list[FT] = []
    if not hit["module_enabled"]:
        notes.append(_module_off(hit["module"], role, lang))
    if not hit["permitted"]:
        notes.append(P(hit["permission_text"], cls="flash flash--warning"))
    if hit["source"] == "third_party":
        notes.append(P(t("help.third_party", lang, module=module_label(hit["module"])), cls="flash"))
    return notes


def _browse(module: str, lang: str) -> FT:
    by_module: dict[str, list[HelpPage]] = {}
    for page in get_index().pages.values():
        if not module or page.module == module:
            by_module.setdefault(page.module, []).append(page)
    sections = [
        Section(H2(t("help.title", lang) if name == CORE else pages[0].module_display),
                Ul(*[_page_link({"id": p.id, "title": p.title, "summary": p.summary})
                     for p in sorted(pages, key=lambda p: (p.order, p.id))], cls="help-list"),
                cls="help-module")
        for name, pages in by_module.items()
    ]
    return Div(H2(t("help.browse", lang)), *sections, cls="help-browse")


async def _results(token: str, q: str, module: str, role: str, lang: str) -> FT:
    found = await api.help_search(token, q, module or None)
    if not found["hits"]:
        return Div(P(found.get("message") or t("help.no_results", lang)), _ask_ai(lang), cls="empty-state")
    return Ul(*[Li(A(h["title"], href=f"/help/{h['id']}", cls="help-list__title"),
                   P(h["summary"], cls="help-list__summary"), *_flags(h, role, lang))
                for h in found["hits"]], cls="help-list")


def _related(ids: list[str], lang: str) -> list[FT]:
    pages = get_index().pages
    links = [Li(A(pages[i].title, href=f"/help/{i}")) for i in ids if i in pages]
    return [H2(t("help.related", lang)), Ul(*links, cls="help-related")] if links else []


def _steps(body: dict) -> FT:
    return Ol(*[Li(P(step["text"]),
                   *([Img(src=step["image_url"], alt=f"{body['title']} {n}", loading="lazy",
                          cls="help-step__img")] if step["image_url"] else []),
                   cls="help-step")
                for n, step in enumerate(body["steps"], start=1)], cls="help-steps")


def setup_routes(app):

    @app.get("/help")
    async def help_index(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse(_LOGIN, status_code=302)
        lang = get_lang(request)
        q = request.query_params.get("q", "").strip()
        module = request.query_params.get("module", "").strip()
        if q:
            try:
                body = await _results(token, q, module, get_role(request), lang)
            except APIError:
                here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
                body = load_error(t("help.load_failed", lang), lang, href=here)
        else:
            body = _browse(module, lang)
        page = await base_shell(
            page_header(t("help.title", lang), _ask_ai(lang)),
            Div(_search_form(q, module, lang), body, cls="help-page"),
            title=t("help.title", lang), nav_active="help", lang=lang, request=request,
        )
        return HTMLResponse(to_xml(page))

    @app.get("/help/for-page")
    async def help_for_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse(_LOGIN, status_code=302)
        lang = get_lang(request)
        try:
            pages = (await api.help_for_route(token, request.query_params.get("path", "/")))["pages"]
            listing: FT = (Ul(*[_page_link(p) for p in pages], cls="help-list") if pages
                           else P(t("help.none_for_this_page", lang), cls="help-panel__none"))
        except APIError:
            listing = P(t("help.load_failed", lang), cls="help-panel__none")
        panel = Div(
            Div(t("help.for_this_page", lang), cls="help-panel__title"),
            listing,
            Div(A(t("help.search_placeholder", lang), href="/help", cls="btn btn--secondary btn--sm"),
                _ask_ai(lang), cls="help-panel__actions"),
            id="topbar-help-panel", cls="help-panel popover-panel", role="dialog",
        )
        return Div(
            Button(t("help.title", lang), type="button", cls="topbar-help__btn popover-toggle",
                   aria_expanded="false", aria_controls="topbar-help-panel"),
            panel, cls="topbar-help",
        )

    @app.get("/help/img/{page_id}/{step}")
    async def help_image(request: Request, page_id: str, step: str):
        if not _token(request):
            return RedirectResponse(_LOGIN, status_code=302)
        page = get_index().pages.get(page_id)
        n = int(step) if step.isdigit() else 0
        if page is None or not 1 <= n <= len(page.steps) or not page.steps[n - 1]["image"]:
            return Response(status_code=404)
        path = page.help_dir / page.steps[n - 1]["image"]
        if not path.is_file():
            return Response(status_code=404)
        return Response(path.read_bytes(), media_type="image/webp",
                        headers={"X-Content-Type-Options": "nosniff"})

    @app.get("/help/{page_id}")
    async def help_page(request: Request, page_id: str):
        token = _token(request)
        if not token:
            return RedirectResponse(_LOGIN, status_code=302)
        lang = get_lang(request)
        status = 200
        try:
            body = await api.help_read(token, page_id)
            content = Div(
                P(t("help.answers_in_your_language", lang), cls="flash"),
                *_flags(body, get_role(request), lang),
                P(body["summary"], cls="help-page__summary"),
                _steps(body),
                *_related(body["related"], lang),
                _back(lang),
                cls="help-page",
            )
            title = body["title"]
        except APIError as e:
            if e.status == 404:
                status, title = 404, t("error.not_found_title", lang)
                content = Div(_back(lang), cls="help-page")
            else:
                title = t("help.title", lang)
                content = Div(load_error(t("help.load_failed", lang), lang, href=request.url.path),
                              _back(lang), cls="help-page")
        page = await base_shell(page_header(title), content, title=title, nav_active="help",
                                lang=lang, request=request)
        return HTMLResponse(to_xml(page), status_code=status)
