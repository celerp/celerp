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
from ui.components.table import display_enum
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


def _match_tags(record: dict, visible: tuple[str, ...]) -> list:
    """`- field: value` tags for the record's search-match reasons (q_match, set by
    the inventory list endpoint) in fields the row does not already display, with
    the matched part of the value bold and long values clipped to a window around
    it. A reason whose field is missing from the record (e.g. hidden by role
    visibility) renders no tag."""
    tags = []
    for reason in record.get("q_match") or []:
        field, match = reason.get("field") or "", str(reason.get("match") or "")
        if not field or not match or field in visible:
            continue
        raw = record.get(field)
        if raw is None or raw == "":
            continue
        value = format(raw, "g") if isinstance(raw, (int, float)) else str(raw)
        i = value.lower().find(match.lower())
        if i < 0:
            continue
        pre, hit, post = value[:i], value[i:i + len(match)], value[i + len(match):]
        if len(pre) > 24:
            pre = "..." + pre[-24:]
        if len(post) > 24:
            post = post[:24] + "..."
        tags.append(Span(f" - {field}: ", pre, Strong(hit), post, cls="search-result-match"))
    return tags


def _status_tag(status: str, domain: str | None):
    """Trailing ` - {status}` chip for a result row. The status is shown via its
    display label (resolved in the request language through display_enum); the raw
    value stays canonical for the active/inactive split done by the caller. An empty
    status renders no chip."""
    label = display_enum(status, domain)
    return Span(f" - {label}", cls="search-result-status") if label else ""


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

        # (coroutine, results_key, icon, label_fn, href_fn, sub_fn,
        # inactive_statuses, status_domain). status_domain is the enum domain used
        # to resolve each result's status label in the request language (display
        # only - the raw status stays canonical for the inactive split below).
        # Items pass status="all" so sold/archived/merged/
        # expired surface in the global bar (the on-page inventory search keeps
        # its active-only default). Each call reuses the module's authenticated
        # wrapper - status="all" filters, it does not bypass the role/company
        # scoping those endpoints enforce. Every list endpoint returns each
        # record's entity id under "id"; detail hrefs link straight to the
        # record's own page. inactive_statuses render greyed and sort after
        # active results.
        descriptors = [
            (api.list_items(token, {"q": q, "limit": "5", "status": "all"}),
             "items", "📦",
             lambda r: r.get("name") or r.get("sku") or "",
             lambda r: f"/inventory/{r.get('id', '')}",
             lambda r: r.get("sku") or "",
             frozenset({"sold", "archived", "merged", "expired", "disposed"}),
             "item_status"),
            (api.list_contacts(token, {"q": q, "limit": "5"}),
             "items", "👤",
             lambda r: r.get("name") or r.get("contact_name") or "",
             lambda r: f"/contacts/{r.get('id', '')}",
             lambda r: "",
             frozenset(),
             "contact_status"),
            (api.list_docs(token, {"q": q, "limit": "5"}),
             "items", "📄",
             lambda r: r.get("doc_number") or r.get("ref") or "",
             lambda r: f"/docs/{r.get('id', '')}",
             lambda r: r.get("doc_type") or "",
             frozenset({"void"}),
             "doc_status"),
            (api.list_mfg_orders(token, {"q": q, "limit": "5"}),
             "items", "🏭",
             lambda r: r.get("description") or r.get("id") or "",
             lambda r: f"/manufacturing/production?q={quote(q)}",
             lambda r: "",
             frozenset({"cancelled"}),
             "mfg_run_status"),
            (api.list_subscriptions(token, {"q": q, "limit": "5"}),
             "items", "🔁",
             lambda r: r.get("name") or r.get("doc_number") or r.get("ref_id") or r.get("id") or "",
             lambda r: f"/subscriptions/{r.get('id', '')}",
             lambda r: "",
             frozenset({"cancelled"}),
             "subscription_status"),
            (api.get_journal(token, {"q": q, "limit": "5"}),
             "entries", "📒",
             lambda r: r.get("memo") or ((r.get("lines") or [{}])[0].get("name") or ""),
             lambda r: "/accounting",
             lambda r: "",
             frozenset(),
             None),
        ]

        responses = await asyncio.gather(*[_safe(d[0]) for d in descriptors])

        # (is_inactive, rendered) pairs; the stable sort below floats every
        # active result above the inactive ones while keeping module order
        # within each group.
        rendered: list[tuple[bool, FT]] = []
        for resp, (_coro, key, icon, label_fn, href_fn, sub_fn, inactive_statuses, status_domain) in zip(responses, descriptors):
            for record in (resp.get(key) or [])[:5]:
                label = label_fn(record)
                if not label:
                    continue
                sub = sub_fn(record)
                status = str(record.get("status") or "").lower()
                inactive = status in inactive_statuses
                rendered.append((inactive, A(
                    f"{icon} {label}",
                    Small(f" ({sub})") if sub else "",
                    *_match_tags(record, ("name", "sku")),
                    _status_tag(status, status_domain),
                    href=href_fn(record),
                    cls="search-result-item search-result-item--inactive" if inactive else "search-result-item",
                )))
        rendered.sort(key=lambda pair: pair[0])
        results: list[FT] = [ft for _inactive, ft in rendered]

        if not results:
            return Div(Span(t("msg.no_results"), cls="search-empty"), cls="search-results-list")

        return Div(*results, cls="search-results-list")
