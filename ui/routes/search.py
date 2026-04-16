# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.config import get_token as _token
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)




def setup_routes(app):

    @app.get("/search")
    async def global_search(request: Request):
        """HTMX partial: search across items, contacts, docs."""
        token = _token(request)
        if not token:
            return Div()
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Div()

        results: list[FT] = []
        try:
            items = (await api.list_items(token, {"q": q, "limit": "5"})).get("items", [])
            for it in items[:5]:
                name = it.get("name", it.get("sku", ""))
                sku = it.get("sku", "")
                results.append(
                    A(f"📦 {name}", Small(f" ({sku})") if sku else "",
                      href=f"/inventory/{it.get('entity_id', '')}", cls="search-result-item")
                )
        except APIError as e:
            logger.warning("search items error: %s", e.detail)

        try:
            contact_resp = await api.list_contacts(token, {"q": q, "limit": "5"})
            for c in contact_resp.get("items", [])[:5]:
                name = c.get("name", c.get("contact_name", ""))
                results.append(
                    A(f"👤 {name}", href=f"/crm/{c.get('entity_id', c.get('id', ''))}", cls="search-result-item")
                )
        except APIError as e:
            logger.warning("search contacts error: %s", e.detail)

        try:
            docs = (await api.list_docs(token, {"q": q, "limit": "5"})).get("items", [])
            for d in docs[:5]:
                ref = d.get("doc_number", d.get("ref", ""))
                dtype = d.get("doc_type", "")
                results.append(
                    A(f"📄 {ref}", Small(f" ({dtype})") if dtype else "",
                      href=f"/docs/{d.get('entity_id', '')}", cls="search-result-item")
                )
        except APIError as e:
            logger.warning("search docs error: %s", e.detail)

        if not results:
            return Div(Span(t("msg.no_results"), cls="search-empty"), cls="search-results-list")

        return Div(*results, cls="search-results-list")
