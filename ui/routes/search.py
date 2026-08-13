# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

import ui.api_client as api
from ui.api_client import APIError
from ui.config import get_token as _token
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)


async def _safe(coro):
    """Run one module's list call in isolation. On an APIError (e.g. a 403 from a
    permission-gated module, or a 500) return an empty response so one module's
    failure never blanks the whole search - the other modules still render."""
    try:
        return await coro
    except APIError as e:
        logger.warning("search module error: %s", e.detail)
        return {}


def setup_routes(app):

    @app.get("/search")
    async def global_search(request: Request):
        """HTMX partial: search WIDE across every primary module (items - all
        statuses, contacts, documents, manufacturing orders, subscriptions and
        journal entries), each through its existing authenticated list endpoint so
        role/company scoping is preserved."""
        token = _token(request)
        if not token:
            return Div()
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Div()

        # (coroutine, results_key, icon, label_fn, href_fn, sub_fn). Items pass
        # status="all" so sold/archived/merged/expired surface in the global bar
        # (the on-page inventory search keeps its active-only default). Each call
        # reuses the module's authenticated wrapper - status="all" filters, it
        # does not bypass the role/company scoping those endpoints enforce.
        descriptors = [
            (api.list_items(token, {"q": q, "limit": "5", "status": "all"}),
             "items", "📦",
             lambda r: r.get("name") or r.get("sku") or "",
             lambda r: f"/inventory/{r.get('entity_id', '')}",
             lambda r: r.get("sku") or ""),
            (api.list_contacts(token, {"q": q, "limit": "5"}),
             "items", "👤",
             lambda r: r.get("name") or r.get("contact_name") or "",
             lambda r: f"/crm/{r.get('entity_id') or r.get('id') or ''}",
             lambda r: ""),
            (api.list_docs(token, {"q": q, "limit": "5"}),
             "items", "📄",
             lambda r: r.get("doc_number") or r.get("ref") or "",
             lambda r: f"/docs/{r.get('entity_id', '')}",
             lambda r: r.get("doc_type") or ""),
            (api.list_mfg_orders(token, {"q": q, "limit": "5"}),
             "items", "🏭",
             lambda r: r.get("description") or r.get("id") or "",
             lambda r: f"/manufacturing/production?q={quote(q)}",
             lambda r: ""),
            (api.list_subscriptions(token, {"q": q, "limit": "5"}),
             "items", "🔁",
             lambda r: r.get("name") or r.get("doc_number") or r.get("ref_id") or r.get("id") or "",
             lambda r: f"/subscriptions/{r.get('entity_id') or r.get('id') or ''}",
             lambda r: ""),
            (api.get_journal(token, {"q": q, "limit": "5"}),
             "entries", "📒",
             lambda r: r.get("memo") or ((r.get("lines") or [{}])[0].get("name") or ""),
             lambda r: "/accounting",
             lambda r: ""),
        ]

        responses = await asyncio.gather(*[_safe(d[0]) for d in descriptors])

        results: list[FT] = []
        for resp, (_coro, key, icon, label_fn, href_fn, sub_fn) in zip(responses, descriptors):
            for record in (resp.get(key) or [])[:5]:
                label = label_fn(record)
                if not label:
                    continue
                sub = sub_fn(record)
                results.append(
                    A(f"{icon} {label}", Small(f" ({sub})") if sub else "",
                      href=href_fn(record), cls="search-result-item")
                )

        if not results:
            return Div(Span(t("msg.no_results"), cls="search-empty"), cls="search-results-list")

        return Div(*results, cls="search-results-list")
