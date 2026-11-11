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
from ui.components.table import display_enum, match_field_label, match_value
from ui.config import get_token as _token
from ui.i18n import t, get_lang
from celerp.services.app_paths import is_app_local_path
from ui.components.encode import safe_url

logger = logging.getLogger(__name__)


def _match_tags(record: dict, visible: tuple[str, ...]) -> list:
    """`- Field: value` tags for the record's search-match reasons (q_match, set by
    the inventory list endpoint) in fields the row does not already display, with
    the matched part of the value bold and long values clipped to a window around
    it. The field reads as its display label, as on the inventory list's "?" icon.
    A reason whose field is missing from the record (e.g. hidden by role
    visibility) renders no tag."""
    tags = []
    for reason in record.get("q_match") or []:
        field, match = reason.get("field") or "", str(reason.get("match") or "")
        if not field or not match or field in visible:
            continue
        value = match_value(record, field)
        if value is None:
            continue
        i = value.lower().find(match.lower())
        if i < 0:
            continue
        pre, hit, post = value[:i], value[i:i + len(match)], value[i + len(match):]
        if len(pre) > 24:
            pre = "..." + pre[-24:]
        if len(post) > 24:
            post = post[:24] + "..."
        tags.append(Span(f" - {match_field_label(field, {})}: ", pre, Strong(hit), post,
                         cls="search-result-match"))
    return tags


def _safe_local_search_href(href) -> str | None:
    """Return href only if it is a non-empty, app-local path, else None.

    The API already holds third-party rows to this shape (`is_app_local_path`),
    but the UI re-checks before rendering a link it did not build, so an
    untrusted module can never place an off-site or script URL in the results
    dropdown.
    """
    if not isinstance(href, str):
        return None
    href = href.strip()
    return href if is_app_local_path(href) else None


def _status_tag(label: str):
    """Trailing ` - {label}` chip for a result row; an empty label renders no chip."""
    return Span(f" - {label}", cls="search-result-status") if label else ""


def _enum_status(domain: str | None):
    """A row's status label from its raw status, in the request language (display_enum)."""
    return lambda record, status: display_enum(status, domain) if domain else ""


def _doc_status(record: dict, status: str) -> str:
    """The status a document or List reads as on its own page and in its table, so one state
    carries one label wherever it is listed (an issued quotation reads Issued, an unpaid invoice
    Awaiting Payment)."""
    from ui.routes.documents import _doc_status_label, _list_status_label
    if record.get("kind") == "list":
        return _list_status_label(record)
    return _doc_status_label(record.get("doc_type") or "", status) if status else ""


def _doc_type_name(record: dict) -> str:
    """The kind of document a result is (Quotation, Invoice, ...), in the request language. A
    List names its list type, a document its document type."""
    if record.get("kind") == "list":
        from ui.routes.documents import _list_type_label
        return _list_type_label(record.get("list_type"))
    from ui.i18n import doc_type_label
    return doc_type_label(record["doc_type"]) if record.get("doc_type") else ""


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
        # status_fn). module is the aggregator's key (the module folder name);
        # result_key is the list field that module's provider returns its rows under.
        # status_fn gives each result's status label in the request language
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
             _enum_status("item_status")),
            ("celerp-contacts", "items", "👤",
             lambda r: r.get("name") or r.get("contact_name") or "",
             lambda r: f"/contacts/{r.get('id', '')}",
             lambda r: "",
             frozenset(),
             _enum_status("contact_status")),
            ("celerp-docs", "items", "📄",
             lambda r: r.get("doc_number") or r.get("ref_id") or r.get("ref") or "",
             lambda r: f"/{'lists' if r.get('kind') == 'list' else 'docs'}/{r.get('id', '')}",
             _doc_type_name,
             frozenset({"void"}),
             _doc_status),
            ("celerp-manufacturing", "items", "🏭",
             lambda r: r.get("description") or r.get("id") or "",
             lambda r: f"/manufacturing/production?q={quote(q)}",
             lambda r: "",
             frozenset({"cancelled"}),
             _enum_status("mfg_run_status")),
            ("celerp-subscriptions", "items", "🔁",
             lambda r: r.get("name") or r.get("doc_number") or r.get("ref_id") or r.get("id") or "",
             lambda r: f"/subscriptions/{r.get('id', '')}",
             lambda r: "",
             frozenset({"cancelled"}),
             _enum_status("subscription_status")),
            ("celerp-accounting", "entries", "📒",
             lambda r: r.get("memo") or ((r.get("lines") or [{}])[0].get("name") or ""),
             lambda r: "/accounting",
             lambda r: "",
             frozenset(),
             _enum_status(None)),
        ]

        # (is_inactive, rendered) pairs; the stable sort below floats every active
        # result above the inactive ones while keeping module order within each group.
        rendered: list[tuple[bool, FT]] = []
        for module, key, icon, label_fn, href_fn, sub_fn, inactive_statuses, status_fn in descriptors:
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
                    _status_tag(status_fn(record, status)),
                    href=safe_url(href_fn(record)),
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
                    href = safe_url(_safe_local_search_href(record.get("href")))
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
