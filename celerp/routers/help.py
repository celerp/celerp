# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Help API: search, browse and read the help pages, serve their screenshots, and list
the pages for one screen.

``help_search`` and ``help_read`` are agent tools (the ``x-celerp-agent`` marker),
so Celerp AI answers how-to questions from the same pages the viewer shows. Help
is readable by every signed-in role: each result says whether the page's module is
on for the company and whether the caller's role holds the permission its screens
need, and never hides a page for either reason.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Annotated
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import AfterValidator

from celerp.help import search as help_search_mod
from celerp.help.registry import CORE, MAX_ADDRESS_LEN, get_index
from celerp.help.render import render_step
from celerp.help.schema import HelpPage, read_image
from celerp.services.auth import get_current_role, get_current_user
from celerp.services.permissions import get_current_company_settings, missing_permission_text
from ui.i18n import current_lang, t

router = APIRouter(dependencies=[Depends(get_current_user)])

_MAX_Q_LEN = 200
_MAX_PATH_LEN = 300
_SEGMENT = re.compile(r"\{[^/{}]+\}")


def _flags(hit: help_search_mod.Hit) -> dict:
    page = hit.page
    return {
        "module": page.module,
        "module_enabled": hit.module_enabled,
        "permitted": hit.permitted,
        "permission_text": None if hit.permitted else missing_permission_text(page.permission),
        "source": page.source,
    }


def _summary(page: HelpPage) -> dict:
    return {"id": page.id, "title": page.title, "summary": page.summary}


@router.get("/help/search", operation_id="help_search", openapi_extra={"x-celerp-agent": True})
async def help_search(
    q: str = Query(..., max_length=_MAX_Q_LEN),
    module: str | None = None,
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
) -> dict:
    """Search the help pages (written in English) for how to do something in Celerp.

    Returns up to 8 hits, best first, each with its id for help_read, whether its
    module is on for this company and whether this role may use its screens.
    """
    query = q.strip()
    if not query:
        raise HTTPException(status_code=422, detail=t("help.empty_query"))
    if module and module not in {p.module for p in get_index().pages.values()}:
        return {"hits": [], "message": t("help.no_module", module=module)}
    hits = help_search_mod.search(query, current_lang(), settings, role, module)
    return {"hits": [{**_summary(h.page), **_flags(h)} for h in hits]}


@router.get("/help/page/{page_id}", operation_id="help_read", openapi_extra={"x-celerp-agent": True})
async def help_read(
    page_id: str,
    role: str = Depends(get_current_role),
    settings: dict = Depends(get_current_company_settings),
) -> dict:
    """Read one help page: its numbered steps, with button and menu names as the user sees them."""
    page = get_index().pages.get(page_id)
    if page is None:
        raise HTTPException(status_code=404, detail=t("error.not_found_title"))
    lang = current_lang()
    return {
        **_summary(page),
        **_flags(help_search_mod.flag(page, settings, role)),
        "steps": [{"text": render_step(step, lang, page.catalogs),
                   "image_url": f"/help/img/{page.id}/{n}" if step["image"] else None}
                  for n, step in enumerate(page.steps, start=1)],
        "related": page.related,
    }


@router.get("/help/browse", include_in_schema=False)
async def help_browse(module: str | None = None) -> dict:
    """Every help page by module (core first, then each module as the index orders them),
    or one module's pages."""
    by_module: dict[str, dict] = {}
    for page in get_index().pages.values():
        if not module or page.module == module:
            group = by_module.setdefault(page.module, {
                "module": page.module, "core": page.module == CORE,
                "display": page.module_display, "pages": []})
            group["pages"].append(page)
    for group in by_module.values():
        group["pages"] = [_summary(p) for p in sorted(group["pages"], key=lambda p: (p.order, p.id))]
    return {"modules": list(by_module.values())}


@router.get("/help/img/{page_id}/{step}", include_in_schema=False)
async def help_image(page_id: str, step: str) -> Response:
    """The screenshot of one step of a help page."""
    page = get_index().pages.get(page_id)
    data = read_image(page, int(step)) if page is not None and step.isdigit() else None
    if data is None:
        raise HTTPException(status_code=404, detail=t("error.not_found_title"))
    return Response(data, media_type="image/webp", headers={"X-Content-Type-Options": "nosniff"})


@lru_cache(maxsize=512)
def _route_pattern(route: str) -> re.Pattern:
    """A help page route's path as a full-path match; each {name} stands for one path segment."""
    parts = _SEGMENT.split(route)
    return re.compile("[^/]+".join(re.escape(p) for p in parts) + r"/?\Z")


def _covers(route: str, path: str, query: set[tuple[str, str]]) -> bool:
    """Whether a help page route covers the screen at ``path`` with ``query``: the paths
    match and every query pair the route names (a tab, say) is on the screen's address."""
    route_path, _, route_query = route.partition("?")
    return (_route_pattern(route_path).match(path) is not None
            and set(parse_qsl(route_query)) <= query)


def _short_path(address: str) -> str:
    if len(address.partition("?")[0]) > _MAX_PATH_LEN:
        raise ValueError(f"the path part is longer than {_MAX_PATH_LEN} characters")
    return address


@router.get("/help/for-route")
async def help_for_route(
    path: Annotated[str, AfterValidator(_short_path), Query(max_length=MAX_ADDRESS_LEN, pattern=r"^/")],
) -> dict:
    """The help pages that cover the screen at ``path`` (its query included), for the top
    bar Help panel. A list address carries its filters, so the query may run long; the
    path part is held to a path's length."""
    path, _, query = path.partition("?")
    pairs = set(parse_qsl(query))
    pages = [p for p in get_index().pages.values() if any(_covers(r, path, pairs) for r in p.routes)]
    pages.sort(key=lambda p: (p.order, p.id))
    return {"pages": [_summary(p) for p in pages]}
