# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import logging
from urllib.parse import quote

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import Response

import ui.api_client as api
from ui.api_client import APIError
from ui.components.table import display_enum
from ui.config import get_token as _token
from ui.i18n import t, get_lang

logger = logging.getLogger(__name__)


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


def _safe_local_search_href(href) -> str | None:
    """Return href only if it is a non-empty, app-local path, else None.

    Accepts a single leading slash only: rejects any scheme (`https:`,
    `javascript:`), protocol-relative `//host`, and the empty string. The API
    already holds third-party rows to this shape, but the UI re-checks before
    rendering a link it did not build, so an untrusted module can never place an
    off-site or script URL in the results dropdown.
    """
    if not isinstance(href, str):
        return None
    href = href.strip()
    if not href.startswith("/") or href.startswith("//"):
        return None
    return href


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
        journal entries).

        One aggregated request to the API's /search router replaces the per-module
        fan-out: the router walks each module's search provider on a single database
        session, gates each on its own permission, and merges the answers under
        {"results": {module: {result_key: [...]}}, "degraded_modules": [...]}. This
        route renders that answer with the same cards, icons, and links as before,
        and degrades honestly: a total failure shows a retry-able error rather than
        an empty 'no results', and a partial failure shows the results it reached
        alongside a plain notice."""
        lang = get_lang(request)
        token = _token(request)
        if not token:
            return Div()
        q = (request.query_params.get("q") or "").strip()
        if len(q) < 2:
            return Div()

        try:
            data = await api.global_search(token, q)
        except APIError as e:
            if e.status == 401:
                # Session no longer valid: send the browser to login instead of
                # swapping a broken fragment into the results dropdown.
                return Response("", status_code=401, headers={"HX-Redirect": "/login"})
            # Every other total failure (503 saturation, 504 timeout, 5xx) is an
            # honest error, not an empty result set. Offer a plain retry.
            logger.warning("global search failed: %s", e.detail)
            return Div(Span(t("search.error", lang), cls="search-empty"), cls="search-results-list")

        results_by_module = data.get("results") or {}
        degraded = data.get("degraded_modules") or []

        # (module, result_key, icon, label_fn, href_fn, sub_fn, inactive_statuses,
        # status_domain). module is the aggregator's key (the module folder name);
        # result_key is the list field that module's provider returns its rows under.
        # status_domain resolves each result's status label in the request language
        # (display only - the raw status stays canonical for the inactive split).
        # inactive_statuses render greyed and sort after active results. Every
        # provider returns each record's entity id under "id"; detail hrefs link
        # straight to the record's own page.
        descriptors = [
            ("celerp-inventory", "items", "📦",
             lambda r: r.get("name") or r.get("sku") or "",
             lambda r: f"/inventory/{r.get('id', '')}",
             lambda r: r.get("sku") or "",
             frozenset({"sold", "archived", "merged", "expired", "disposed"}),
             "item_status"),
            ("celerp-contacts", "items", "👤",
             lambda r: r.get("name") or r.get("contact_name") or "",
             lambda r: f"/contacts/{r.get('id', '')}",
             lambda r: "",
             frozenset(),
             "contact_status"),
            ("celerp-docs", "items", "📄",
             lambda r: r.get("doc_number") or r.get("ref") or "",
             lambda r: f"/docs/{r.get('id', '')}",
             lambda r: r.get("doc_type") or "",
             frozenset({"void"}),
             "doc_status"),
            ("celerp-manufacturing", "items", "🏭",
             lambda r: r.get("description") or r.get("id") or "",
             lambda r: f"/manufacturing/production?q={quote(q)}",
             lambda r: "",
             frozenset({"cancelled"}),
             "mfg_run_status"),
            ("celerp-subscriptions", "items", "🔁",
             lambda r: r.get("name") or r.get("doc_number") or r.get("ref_id") or r.get("id") or "",
             lambda r: f"/subscriptions/{r.get('id', '')}",
             lambda r: "",
             frozenset({"cancelled"}),
             "subscription_status"),
            ("celerp-accounting", "entries", "📒",
             lambda r: r.get("memo") or ((r.get("lines") or [{}])[0].get("name") or ""),
             lambda r: "/accounting",
             lambda r: "",
             frozenset(),
             None),
        ]

        # (is_inactive, rendered) pairs; the stable sort below floats every active
        # result above the inactive ones while keeping module order within each group.
        rendered: list[tuple[bool, FT]] = []
        for module, key, icon, label_fn, href_fn, sub_fn, inactive_statuses, status_domain in descriptors:
            bucket = (results_by_module.get(module) or {}).get(key) or []
            for record in bucket[:5]:
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
        # Generic third-party modules: any module the aggregator returned that the
        # first-party descriptors above do not render. Its provider rows are
        # canonical ({id, label, href, subtitle}); render each as a plain labelled
        # link, escaping every field (FastHTML escapes str children) and re-checking
        # the href is app-local before trusting a link this app did not build.
        known = {module for module, *_rest in descriptors}
        for module, buckets in results_by_module.items():
            if module in known:
                continue
            for _key, rows in (buckets or {}).items():
                for record in (rows or [])[:5]:
                    label = record.get("label") or ""
                    href = _safe_local_search_href(record.get("href"))
                    if not label or not href:
                        continue
                    subtitle = record.get("subtitle") or ""
                    rendered.append((False, A(
                        f"🔎 {label}",
                        Small(f" ({subtitle})") if subtitle else "",
                        href=href,
                        cls="search-result-item",
                    )))

        rendered.sort(key=lambda pair: pair[0])
        results: list[FT] = [ft for _inactive, ft in rendered]

        if not results:
            # No matches. If some modules could not be searched, that is a failure,
            # not a genuine empty result: show the retry-able error. Only a clean
            # search that truly matched nothing uses the no-results message.
            if degraded:
                return Div(Span(t("search.error", lang), cls="search-empty"), cls="search-results-list")
            return Div(Span(t("msg.no_results", lang), cls="search-empty"), cls="search-results-list")

        children: list[FT] = list(results)
        if degraded:
            # Some modules answered and some could not. Show what we reached, with a
            # plain notice so the list is never silently partial.
            children.append(Span(t("search.partial", lang), cls="search-partial"))
        return Div(*children, cls="search-results-list")
