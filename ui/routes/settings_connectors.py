# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Web Access: Connectors tab."""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from fasthtml.common import *
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from ui.components.attrs import hx_vals
from ui.components.shell import secret_input
from ui.components.activity import relative_time
from ui.i18n import get_lang, refusal_text, t
from ui.routes.settings import _check_permission, _token, authorize_in_process
from ui.security import is_safe_authorize_url

from celerp.accounting_roles import PERIOD_LOCK_PATH
from celerp.connectors.base import (
    INBOUND_ENTITIES, OUTBOUND_ENTITIES, ConnectorCategory, SyncFrequency,
)
from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY
from celerp.services.background import spawn_background

log = logging.getLogger(__name__)


def _store_url_error(store_url: str, platform: str) -> str | None:
    """Validate an API-key connector's store URL. Returns a translation key for the
    error, or None if valid.

    The consumer key/secret are sent to the store as HTTP Basic Auth, so the URL
    must be https (cleartext http would expose them); http is allowed only behind
    the CELERP_ALLOW_HTTP_STORE dev override. WooCommerce additionally requires a
    store URL (it has no other endpoint)."""
    if store_url:
        allow_http = store_url.startswith("http://") and bool(os.environ.get("CELERP_ALLOW_HTTP_STORE"))
        if not (store_url.startswith("https://") or allow_http):
            return "connectors.store_url_must_use_https"
    elif platform in ("woocommerce", "celerp"):
        return "connectors.store_url_required"
    if platform == "celerp" and store_url:
        from celerp.services.peer_access import is_https_origin
        if not is_https_origin(store_url):
            return "connectors.store_url_must_use_https"
    return None

# Where a client sends its accountant to learn how Celerp works for firms.
ACCOUNTANTS_URL = "https://celerp.com/accountants"

_CONNECTOR_ICONS: dict[str, str] = {
    "shopify": "🛍️",
    "woocommerce": "🛒",
    "quickbooks": "📊",
    "xero": "📗",
    "celerp": "📚",
}

_DEFAULT_FREQUENCY: dict[str, SyncFrequency] = {
    ConnectorCategory.WEBSITE.value: SyncFrequency.REALTIME,
    ConnectorCategory.ACCOUNTING.value: SyncFrequency.MANUAL,
}

_VALID_PLATFORMS: set[str] | None = None


def _valid_platforms() -> set[str]:
    global _VALID_PLATFORMS
    if _VALID_PLATFORMS is None:
        from celerp.connectors import all_connectors
        _VALID_PLATFORMS = {c.name for c in all_connectors()}
    return _VALID_PLATFORMS


def _validate_platform(platform: str):
    """Return error response if platform is invalid, else None."""
    if platform not in _valid_platforms():
        return Span(t("connectors.unknown_connector", platform=platform), cls="flash flash--warning")
    return None


# Raw SyncRun status enum values shown to the user. Each maps to an
# ``enum.sync_status.<value>`` key resolved at render time (see _sync_status_label),
# so the label translates while the raw value stays canonical everywhere else.
_SYNC_STATUS_VALUES: tuple[str, ...] = ("success", "partial", "failed")
_SYNC_STATUS_LABELS: dict[str, str] = {v: f"enum.sync_status.{v}" for v in _SYNC_STATUS_VALUES}


def _sync_status_label(status: str) -> str:
    """Translated display label for a raw SyncRun status, resolved at render time.
    Unknown values degrade honestly to the raw string."""
    key = _SYNC_STATUS_LABELS.get(status)
    return t(key) if key else (status or "")


# Raw error text a sync stores (exception messages, kept in English) -> the plain reason
# shown on the connector's page, matched in order on the text after any record label
# and "... API error:" style prefix. Anything unmatched gets connectors.fail_generic.
_FAILURE_REASONS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), key) for pattern, key in (
        (r"already in progress", "connectors.fail_busy"),
        (r"connection changed while", "connectors.fail_connection_changed"),
        (r"blocked by direction", "connectors.fail_direction"),
        (r"does not support", "connectors.fail_unsupported"),
        (r"update celerp to continue", "connectors.fail_upgrade"),
        (r"records came from it", "connectors.fail_store_unchecked"),
        (r"does not have them", "connectors.fail_store_changed"),
        (r"credentials are temporarily unavailable", "connectors.fail_credentials"),
        (r"realm_id", "connectors.fail_credentials"),
        (r"key was revoked or has expired", "connectors.fail_key_closed"),
        (r"\b40[13]\b|unauthori[sz]ed|forbidden|invalid.{0,20}(token|key|credential)", "connectors.fail_access"),
        (r"\b429\b|rate.?limit|too many requests", "connectors.fail_rate_limited"),
        (r"timed? ?out|timeout|connecterror|connection (refused|reset|error)|name or service|"
         r"name resolution|nodename|unreachable|all connection attempts", "connectors.fail_unreachable"),
        (r"\b5\d\d\b|server error|bad gateway|service unavailable", "connectors.fail_remote"),
        (r"invalid stock quantity", "connectors.fail_stock"),
    )
)
# A record-level error starts with what it was about ("SKU ABC-1: ...", "WooCommerce
# product 12: ..."); the record stays in front of the reason.
_RECORD_PREFIX = re.compile(r"^((?:[A-Za-z]+ )?(?:SKU|Invoice|Customer|Contact|Item|Product)\b[^:]*): (.*)$", re.DOTALL)
_SYNC_REASONS_ID = "connector-sync-reasons"
_ATTENTION_ID = "connector-attention"


def sync_failure_reason(raw: str, service: str, lang: str | None = None) -> str:
    """The plain reason, with what to do, for one error a *service* sync stored. Never
    echoes the raw text, which is an exception message, not something to act on."""
    text = str(raw or "")
    record = _RECORD_PREFIX.match(text)
    cause = record.group(2) if record else text
    key = next((k for pattern, k in _FAILURE_REASONS if pattern.search(cause)), "connectors.fail_generic")
    reason = t(key, lang, service=service)
    return f"{record.group(1)}: {reason}" if record else reason


def _sync_reasons(runs: dict, service: str, lang: str = "en") -> FT:
    """Why the latest sync of each record type had problems, in plain words, shown under
    the status table; the Failed and Issues badges link here. Identical reasons are
    listed once."""
    items = []
    for e in _ENTITY_ORDER:
        r = runs.get(e)
        if r is None or getattr(r, "finished_at", None) is None:
            continue
        reasons = dict.fromkeys(sync_failure_reason(x, service, lang) for x in (getattr(r, "errors", None) or []))
        items += [Li(Strong(_entity_label(e, lang)), ": ", reason) for reason in reasons]
    if not items:
        return Span()
    return Div(
        P(t("connectors.fail_reasons_header", lang), cls="settings-section-title"),
        Ul(*items),
        id=_SYNC_REASONS_ID,
        cls="connector-sync-reasons",
    )


def _changes(run, lang: str | None = None) -> str:
    """What a sync run added and updated, in words."""
    return t("connectors.changes", lang, added=f"{run.created_count:,}", updated=f"{run.updated_count:,}")


def _last_sync_info(run, attention: int = 0, href: str = "") -> FT:
    """Compact summary of the latest SyncRun, or a 'never synced' note. Uses the shared
    relative_time() formatter and thousands-separated counts for consistency with the
    rest of the app. ``attention`` counts records still waiting on a person, which
    may have been left by an earlier run."""
    if run is None:
        return Span(t("connectors.never_synced", default="Never synced"), cls="connector-sync-info")
    if run.finished_at is None:
        return Span(t("connectors.syncing", default="Syncing…"),
                    cls="connector-sync-info connector-sync-info--running")
    finished = run.finished_at
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    status_cls = {
        "success": "connector-sync-info--ok",
        "partial": "connector-sync-info--warn",
        "failed": "connector-sync-info--err",
    }.get(run.status, "")
    counts = _changes(run)
    status = _sync_status_label(run.status)
    if href and run.status in ("failed", "partial") and getattr(run, "errors", None):
        status = A(status, href=f"{href}#{_SYNC_REASONS_ID}")
    elif run.status == "success" and attention:
        # The sync finished, but records it processed are still waiting on a person.
        status_cls = "connector-sync-info--warn"
        status = t("connectors.needs_attention")
        if href:
            status = A(status, href=f"{href}#{_ATTENTION_ID}")
    parts = [relative_time(finished.isoformat()), status, counts]
    if attention:
        parts.append(t("connectors.attention_count", n=attention))
    joined = [parts[0]]
    for part in parts[1:]:
        joined += [" · ", part]
    return Span(*joined, cls=f"connector-sync-info {status_cls}")


def _direction_toggle(cid: str, current: str, lang: str = "en", detail: bool = False) -> FT:
    """Three-option sync direction toggle: Inbound / Outbound / Both. On the detail
    page (`detail`) it swaps itself; on the connectors list, the connector's card."""
    options = [
        ("inbound", t("connectors.dir_inbound", lang, default="Inbound")),
        ("outbound", t("connectors.dir_outbound", lang, default="Outbound")),
        ("both", t("connectors.dir_both", lang, default="Both")),
    ]
    buttons = []
    for val, label in options:
        active = "btn--primary" if val == current else "btn--outline"
        buttons.append(
            Button(
                label,
                cls=f"btn btn--xs {active}",
                hx_post=f"/settings/connectors/{cid}/direction" + ("?detail=1" if detail else ""),
                hx_vals=hx_vals({"direction": val}),
                hx_target="closest .connector-direction-toggle" if detail else f"#connector-card-{cid}",
                hx_swap="outerHTML",
            )
        )
    return Div(
        Span(t("connectors.direction", lang, default="Direction") + ": ", cls="connector-label"),
        *buttons,
        cls="connector-direction-toggle",
    )


def _frequency_select(cid: str, current: str, lang: str = "en", detail: bool = False) -> FT:
    """Sync frequency selector for accounting connectors; swaps like _direction_toggle."""
    options = [
        (SyncFrequency.MANUAL.value, t("connectors.freq_manual", lang, default="Manual")),
        (SyncFrequency.DAILY.value, t("connectors.freq_daily", lang, default="Daily")),
    ]
    opts = [Option(label, value=val, selected=(val == current)) for val, label in options]
    return Div(
        Span(t("connectors.frequency", lang, default="Sync") + ": ", cls="connector-label"),
        Select(
            *opts,
            name="sync_frequency",
            cls="select select--sm",
            hx_post=f"/settings/connectors/{cid}/frequency" + ("?detail=1" if detail else ""),
            hx_target="closest .connector-frequency-select" if detail else f"#connector-card-{cid}",
            hx_swap="outerHTML",
            hx_include="closest div",
        ),
        cls="connector-frequency-select",
    )


async def _fetch_catalog(relay_url: str, instance_id: str, token: str = "") -> tuple[list[dict], str, bool]:
    """Fetch connector catalog via API process proxy (which holds the gateway token).
    Returns (connectors, error_detail, needs_plan) - error_detail is "" on
    success; needs_plan means the relay refused with 402 (no entitled plan)."""
    from ui.api_client import get_connectors_catalog
    try:
        return await get_connectors_catalog(token)
    except Exception as exc:
        return [], str(exc), False


async def _get_last_runs(company_id: str) -> dict[str, object]:
    """Return latest SyncRun per connector platform."""
    from celerp.db import get_session_ctx
    from celerp.models.sync_run import SyncRun
    import sqlalchemy as sa

    result: dict[str, object] = {}
    try:
        async with get_session_ctx() as session:
            rows = await session.execute(
                sa.select(SyncRun)
                .where(
                    SyncRun.company_id == company_id,
                    SyncRun.entity != CONNECTOR_RESET_ENTITY,
                )
                .order_by(SyncRun.started_at.desc())
            )
            seen: set[str] = set()
            for (run,) in rows:
                if run.connector not in seen:
                    result[run.connector] = run
                    seen.add(run.connector)
    except Exception:
        pass
    return result


async def _attention(company_id: str, platform: str) -> list[dict]:
    """Records waiting on a person for one connector. The page still renders
    when the list cannot be read; the failure is logged."""
    from celerp.connectors.sync_runner import attention_entries
    try:
        return await attention_entries(company_id, platform)
    except Exception:
        log.warning("could not read the attention list for %s", platform, exc_info=True)
        return []


def _open_attention_count(entries: list[dict]) -> int:
    """Entries not yet marked reconciled."""
    return sum(1 for e in entries if not e.get("reconciled"))


def _open_attention_by_entity(entries: list[dict]) -> dict[str, int]:
    """Entries not yet marked reconciled, counted per entity."""
    counts: dict[str, int] = {}
    for e in entries:
        if not e.get("reconciled"):
            counts[e.get("entity", "")] = counts.get(e.get("entity", ""), 0) + 1
    return counts


def _refusal(exc: Exception) -> str:
    """What a refused connector change tells the user: the ownership reason, or the
    API's reason when the caller's access changed before the write (403)."""
    return str(getattr(exc, "detail", None) or exc)


def _request_company_id(request: Request) -> str:
    """Return the API-verified company id cached by the permission gate."""
    company = getattr(request.state, "auth_company", None)
    company_id = company.get("id") if isinstance(company, dict) else None
    if not company_id:
        raise RuntimeError(t("error.company_unavailable"))
    return str(company_id)


async def _get_connector_config(company_id: str, connector: str):
    """Return company-owned state, adopting a lone legacy row safely."""
    import sqlalchemy as sa
    from celerp.config import ensure_instance_id
    from celerp.connectors.ownership import ConnectorOwnershipError, claim_connector_ownership
    from celerp.db import get_session_ctx
    from celerp.models.company import Company
    from celerp.models.connector_config import ConnectorConfig

    async with get_session_ctx() as session:
        row = await session.scalar(
            sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == str(company_id),
                ConnectorConfig.connector == connector,
            ).limit(1)
        )
        if row is not None:
            return row

        legacy_id = ensure_instance_id()
        companies = (await session.execute(sa.select(Company.id).limit(2))).scalars().all()
        if (
            legacy_id != str(company_id)
            and len(companies) == 1
            and str(companies[0]) == str(company_id)
        ):
            try:
                row = await claim_connector_ownership(
                    session, company_id, connector, create=False
                )
                await session.commit()
                return row
            except ConnectorOwnershipError:
                await session.rollback()
        return None


def _local_connector_entry(platform: str) -> dict:
    """Build display metadata for a locally known connector."""
    from celerp.connectors.registry import get as get_connector

    connector = get_connector(platform)
    category = getattr(connector.category, "value", connector.category)
    return {
        "id": platform,
        "name": connector.display_name,
        "category": category,
        "auth_type": "api_key" if platform in ("woocommerce", "celerp") else "oauth",
        "connected": False,
        "entities": [
            getattr(entity, "value", entity)
            for entity in connector.supported_entities
        ],
    }


async def _owned_connector_configs(company_id: str) -> list:
    """Return connector configuration rows owned by the current company."""
    import sqlalchemy as sa
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig

    async with get_session_ctx() as session:
        return list((await session.execute(
            sa.select(ConnectorConfig).where(
                ConnectorConfig.company_id == str(company_id)
            )
        )).scalars().all())


async def _kickoff_connector_sync(company_id: str, platform: str, token: str) -> None:
    """Start the connector's canonical sync plan in the API process."""
    from ui.api_client import start_connector_sync
    result = await start_connector_sync(token, platform)
    if not result.get("ok"):
        raise RuntimeError(refusal_text(result.get("detail")) or "Could not start connector sync")


async def _autosync_once(company_id: str, platform: str, token: str) -> None:
    """Best-effort background sync kickoff that never raises into a render path."""
    try:
        await _kickoff_connector_sync(company_id, platform, token)
    except Exception:
        log.warning("auto-sync on first view failed (non-fatal) for %s", platform, exc_info=True)


# "How it works" steps per connector. Values are i18n keys resolved at render time
# (see _connector_card), so the steps translate to the request language. The shared
# accounting-sync step (quickbooks + xero) reuses one key.
_HOW_IT_WORKS: dict[str, list[str]] = {
    "shopify": [
        "connectors.how_shopify_1",
        "connectors.how_shopify_2",
        "connectors.how_shopify_3",
    ],
    "woocommerce": [
        "connectors.how_woocommerce_1",
        "connectors.how_woocommerce_2",
        "connectors.how_woocommerce_3",
    ],
    "quickbooks": [
        "connectors.how_quickbooks_1",
        "connectors.how_quickbooks_2",
        "connectors.how_accounting_sync",
    ],
    "xero": [
        "connectors.how_xero_1",
        "connectors.how_xero_2",
        "connectors.how_accounting_sync",
    ],
}

# Sync entity types -> display-label i18n key, resolved at render time (see
# _entity_label). Common entities reuse existing app-wide labels; push-direction and
# products get connector-scoped keys. The raw entity value stays canonical elsewhere.
_ENTITY_LABELS: dict[str, str] = {
    "products": "connectors.entity_products",
    "orders": "dashboard.orders",
    "contacts": "dashboard.contacts",
    "inventory": "nav.inventory",
    "journals": "connectors.entity_journals",
    "period_lock": "connectors.entity_period_lock",
    "products_out": "connectors.entity_products_out",
    "contacts_out": "dashboard.contacts",
    "journals_out": "connectors.entity_journals_out",
    "invoices_out": "connectors.entity_invoices_out",
    "documents_out": "connectors.entity_documents_out",
    "inventory_out": "connectors.entity_inventory_out",
    "attachments_out": "connectors.entity_attachments_out",
}


def _entity_label(entity: str, lang: str = "en") -> str:
    """Translated display label for a sync entity type, resolved at render time.
    Unknown entities degrade honestly to their raw value."""
    key = _ENTITY_LABELS.get(entity)
    return t(key, lang) if key else entity

_ENTITY_ORDER = [*INBOUND_ENTITIES, *OUTBOUND_ENTITIES]


async def _entity_runs(company_id: str, connector: str) -> dict:
    """Latest SyncRun per entity for a connector (entity -> SyncRun). Used by the detail
    view and the polling status fragment."""
    import sqlalchemy as sa

    from celerp.db import get_session_ctx
    from celerp.models.sync_run import SyncRun

    out: dict = {}
    try:
        async with get_session_ctx() as session:
            rows = await session.execute(
                sa.select(SyncRun)
                .where(
                    SyncRun.company_id == company_id,
                    SyncRun.connector == connector,
                    SyncRun.entity != CONNECTOR_RESET_ENTITY,
                )
                .order_by(SyncRun.started_at.desc())
            )
            for (run,) in rows:
                out.setdefault(run.entity, run)
    except Exception:
        pass
    return out


def _any_in_progress(runs: dict) -> bool:
    """True if any entity has an unfinished (in-progress) run."""
    return any(getattr(r, "finished_at", True) is None for r in runs.values())


def _overall_status(runs: dict) -> str:
    """Aggregate status across entities (for the completion toast)."""
    statuses = {getattr(r, "status", "") for r in runs.values()}
    if "failed" in statuses:
        return "failed"
    if "partial" in statuses:
        return "partial"
    return "success"


def _sync_complete_toast(runs: dict, attention: list[dict], service: str,
                         lang: str = "en") -> tuple[str, str]:
    """The toast for a finished sync, and its kind: a sync that finished with records
    still waiting on a person says how many, so it never reads as all done."""
    if _overall_status(runs) == "failed":
        return t("connectors.sync_failed", lang, service=service), "error"
    if (waiting := _open_attention_count(attention)):
        return t("connectors.sync_complete_attention", lang, n=waiting), "warning"
    return t("connectors.sync_complete", lang), "success"


def _status_badge_for(status: str, lang: str = "en", has_reasons: bool = False,
                      attention: int = 0) -> FT:
    """Status badge; a Failed or Issues badge with reasons links to them below the table.
    A sync that finished with ``attention`` records waiting on a person says Needs
    attention with the count, linked to the list."""
    if status == "success" and attention:
        return A(t("connectors.status_needs_attention", lang, n=attention),
                 href=f"#{_ATTENTION_ID}", cls="badge badge--draft")
    cls = {"success": "badge--active", "partial": "badge--draft",
           "failed": "badge--overdue", "running": "badge--inactive"}.get(status, "badge--inactive")
    label = {"success": t("connectors.status_ok", lang), "partial": t("connectors.status_partial", lang),
             "failed": t("connectors.status_failed", lang), "running": t("connectors.syncing", lang)}.get(
        status, status or "-")
    if has_reasons and status in ("failed", "partial"):
        return A(label, href=f"#{_SYNC_REASONS_ID}", cls=f"badge {cls}")
    return Span(label, cls=f"badge {cls}")


def _entity_status_table(runs: dict, lang: str = "en", attention: dict[str, int] | None = None) -> FT:
    """Per-entity sync status table. Shared by the detail view and the polling fragment.
    ``attention`` counts each entity's records still waiting on a person."""
    attention = attention or {}
    present = [e for e in _ENTITY_ORDER if e in runs]
    if not present:
        return P(t("connectors.never_synced", lang), cls="empty-state-msg")
    rows = []
    for e in present:
        r = runs[e]
        running = getattr(r, "finished_at", None) is None
        when = t("connectors.syncing", lang) if running else relative_time(r.finished_at.isoformat())
        counts = "…" if running else _changes(r, lang)
        errs = list(getattr(r, "errors", None) or [])
        rows.append(Tr(
            Td(_entity_label(e, lang)),
            Td(when),
            Td(_status_badge_for("running" if running else r.status, lang, has_reasons=bool(errs),
                                 attention=attention.get(e, 0))),
            Td(counts),
            Td(str(len(errs)) if errs else "--", cls="cell--number"),
            cls="data-row",
        ))
    return Table(
        Thead(Tr(Th(t("connectors.entity", lang)), Th(t("connectors.last_sync", lang)),
                 Th(t("connectors.status", lang)),
                 Th(t("connectors.changes_header", lang)),
                 Th(t("connectors.issues_header", lang), cls="cell--number"))),
        Tbody(*rows),
        cls="data-table",
    )


def _attention_item(platform: str, entry: dict, lang: str = "en", error: str | None = None) -> FT:
    """One record waiting on a person. A change only a person can reconcile
    (it carries a signature) offers Mark reconciled, and Undo once marked; one the lock
    date holds links to Period lock. ``error`` explains why the last Mark or Undo did not apply."""
    record_id = str(entry.get("id", ""))
    item_id = f"connector-attention-{platform}-{record_id}"
    action = Span()
    if entry.get("signature"):
        url = f"/settings/connectors/{platform}/attention/{record_id}/reconciled"
        common = {"hx_target": f"#{item_id}", "hx_swap": "outerHTML"}
        if entry.get("reconciled"):
            action = Span(
                " ", Em(t("connectors.attention_reconciled", lang)), " ",
                Button(t("btn.undo", lang), cls="btn btn--xs btn--outline",
                       hx_delete=url, **common),
            )
        else:
            action = Span(" ", Button(
                t("connectors.attention_mark_reconciled", lang),
                cls="btn btn--xs btn--outline", hx_post=url,
                hx_vals=hx_vals({"signature": entry["signature"]}), **common,
            ))
    from ui.components.shell import flash
    locked = Span(" ", t("connectors.attention_locked", lang), " ",
                  A(t("page.period_lock", lang), href=PERIOD_LOCK_PATH)) if entry.get("locked") else Span()
    return Li(
        Strong(str(entry.get("label", record_id))), ": ", str(entry.get("reason", "")),
        locked, action, flash(error) if error else Span(), id=item_id,
    )


def _attention_list(platform: str, entries: list[dict], lang: str = "en") -> FT:
    """Records the connector could not import and is holding for a person (an
    order whose customer or product is missing, a refund to reconcile, or a record
    the receiving company could not take, named by its number or name). The
    list is kept until a sync produces a new one, so a failed run never hides it.
    Orders a person marked reconciled sit in a collapsed section under the open
    ones, with Undo, until the order changes in the store."""
    if not entries:
        return Span()
    open_entries = [e for e in entries if not e.get("reconciled")]
    reconciled = [e for e in entries if e.get("reconciled")]
    return Div(
        # A store connector holds orders; the Celerp connector holds any record it sends.
        P(t("connectors.attention_header_records" if platform == "celerp"
            else "connectors.attention_header", lang), cls="settings-section-title"),
        Ul(*[_attention_item(platform, e, lang) for e in open_entries]) if open_entries else Span(),
        Details(
            Summary(t("connectors.attention_reconciled_header", lang, n=len(reconciled)),
                    cls="text-muted mt-md"),
            Ul(*[_attention_item(platform, e, lang) for e in reconciled]),
        ) if reconciled else Span(),
        id=_ATTENTION_ID,
        cls="connector-attention",
    )


def _connector_status_view(
    platform: str, runs: dict, lang: str = "en", force_poll: bool = False,
    attention: list[dict] = (),
) -> FT:
    """Status table wrapped in a self-re-triggering container. While a sync is in progress
    (or force_poll, right after kicking one off) the fragment re-fetches itself every 2s
    via the one-shot `load delay` idiom - fine here because the polled endpoint is the
    local app, so requests succeed and each swap re-arms the trigger (an `every` trigger
    is needed only where a request can fail mid-poll, as in the modules restart panel);
    when all entities are terminal it renders WITHOUT the trigger, stopping the poll.
    aria-live announces it."""
    from celerp.connectors.registry import service_name
    polling = force_poll or _any_in_progress(runs)
    attrs: dict = {}
    if polling:
        attrs = {"hx_get": f"/settings/connectors/{platform}/status?polling=1",
                 "hx_trigger": "load delay:2s", "hx_swap": "outerHTML"}
    return Div(
        _entity_status_table(runs, lang, _open_attention_by_entity(attention)),
        _sync_reasons(runs, service_name(platform), lang),
        _attention_list(platform, list(attention), lang),
        id=f"connector-status-{platform}",
        cls="connector-status-view",
        **{"aria-live": "polite"},
        **attrs,
    )


def _deposit_select_row(cid: str, current: str, bank_accounts: list[dict], lang: str = "en") -> FT:
    """Which account payments recorded from this store's orders are booked to.
    Blank keeps the company's online-payments default; the searchable list is
    the same active bank accounts the payments settings page offers."""
    from ui.components.table import searchable_select

    options = [("", t("connectors.deposit_default", lang))] + [
        (ba.get("chart_account_code", ""),
         f"{ba.get('chart_account_code', '')} - {ba.get('bank_name', '')}")
        for ba in bank_accounts
    ] + [("__new__", t("acct.add_bank_account", lang))]
    return Div(
        Span(t("connectors.deposit_label", lang) + ": ", cls="connector-label"),
        searchable_select(
            "woocommerce_deposit_account", options, value=current,
            aria_label=t("connectors.deposit_label", lang),
            hx_post=f"/settings/connectors/{cid}/deposit-account",
            hx_trigger="change",
            hx_target=f"#connector-deposit-{cid}",
            hx_swap="outerHTML",
        ),
        id=f"connector-deposit-{cid}",
        cls="connector-deposit-select",
    )


def _connector_detail_body(
    c: dict, runs: dict, config, lang: str = "en", deposit: dict | None = None,
    attention: list[dict] = (),
) -> FT:
    """The reusable detail body (header actions + config + live status table). Used by the
    full-page detail route. ``deposit`` ({"current", "bank_accounts"}) renders the
    payment deposit account row for stores whose orders record payments."""
    cid = c["id"]
    category = c.get("category", "website")
    frequency = config.sync_frequency if config else _DEFAULT_FREQUENCY.get(category, SyncFrequency.MANUAL).value

    sync_btn = Button(
        Span(t("connectors.sync_now", lang)), Span(cls="btn-spinner"),
        cls="btn btn--sm btn--primary",
        hx_post=f"/settings/connectors/{cid}/sync",
        hx_target=f"#connector-status-{cid}",
        hx_swap="outerHTML",
        hx_disabled_elt="this",
    )
    disconnect_btn = Button(
        t("connectors.disconnect", lang),
        cls="btn btn--sm btn--outline btn--danger", style="margin-left:8px;",
        hx_delete=f"/settings/connectors/{cid}/disconnect?redirect=1",
        hx_confirm=_disconnect_confirm(cid, lang),
        hx_target=f"#connector-disconnect-result-{cid}",
        hx_swap="innerHTML",
    )
    config_rows = [_direction_toggle(cid, config.direction if config else "both", lang, detail=True)]
    if category == ConnectorCategory.ACCOUNTING.value:
        config_rows.append(_frequency_select(cid, frequency, lang, detail=True))
    if deposit is not None:
        config_rows.append(_deposit_select_row(cid, deposit["current"], deposit["bank_accounts"], lang))

    return Div(
        Div(sync_btn, disconnect_btn, cls="connector-action-area"),
        Div(id=f"connector-disconnect-result-{cid}"),
        Div(*config_rows, cls="connector-connected-details"),
        P(t("connectors.status", lang), cls="settings-section-title"),
        _connector_status_view(cid, runs, lang, attention=attention),
        P(A(t("connectors.view_synced"), href=f"/inventory?source={cid}",
            cls="btn btn--sm btn--outline"), cls="connector-synced-link"),
        cls="settings-card",
    )


def _disconnect_confirm(platform: str, lang: str, *, anyway: bool = False) -> str:
    """What a Disconnect asks before it goes. The accountant connection has no store and
    no webhooks, so it asks in its own words."""
    key = "disconnect_anyway_confirm" if anyway else "disconnect_confirm"
    return t(f"connectors.{'peer_' if platform == 'celerp' else ''}{key}", lang)


def _disconnect_failed(
    result: dict, lang: str, *, platform: str, force: bool, retry: str, target: str, swap: str,
    **wrapper,
) -> FT:
    """The outcome of a disconnect that changed nothing. Unless it was already
    forced, it offers to disconnect anyway, with a warning."""
    detail = refusal_text(result.get("detail")) or result.get("error") or "disconnect_failed"
    return Div(
        Span(t("connectors.disconnect_failed", lang, detail=detail), cls="flash flash--warning"),
        *([] if force else [Button(
            t("connectors.disconnect_anyway", lang),
            cls="btn btn--danger btn--sm",
            hx_delete=retry,
            hx_target=target,
            hx_swap=swap,
            hx_confirm=_disconnect_confirm(platform, lang, anyway=True),
        )]),
        **wrapper,
    )


def _connector_card(
    c: dict,
    last_run,
    relay_url: str,
    instance_id: str,
    config=None,
    lang: str = "en",
    attention: int = 0,
) -> FT:
    cid = c["id"]
    coming_soon = c.get("status") == "coming-soon"
    connected = c.get("connected", False)
    ownership = c.get("ownership")
    ambiguous = ownership == "ambiguous"
    # An authorization this company started that never finished.
    pending = (
        config is not None and not connected and not ambiguous
        and c.get("auth_type") == "oauth"
    )
    icon = _CONNECTOR_ICONS.get(cid, "🔌")
    category = c.get("category", "website")
    name = c.get("name", cid)
    description = c.get("description", "")
    entities = c.get("entities", [])

    frequency = config.sync_frequency if config else _DEFAULT_FREQUENCY.get(category, SyncFrequency.MANUAL).value

    # ── Status badge ──────────────────────────────────────────────────────────
    if connected:
        status_badge = Span("● " + t("connectors.connected", lang), cls="connector-badge connector-badge--connected")
    elif pending:
        status_badge = Span("◐ " + t("connectors.pending_title", lang), cls="connector-badge connector-badge--idle")
    elif coming_soon:
        status_badge = Span(t("connectors.coming_soon", lang), cls="connector-badge connector-badge--soon")
    else:
        status_badge = Span("○ " + t("connectors.not_connected", lang), cls="connector-badge connector-badge--idle")

    # ── Synced entities pills ─────────────────────────────────────────────────
    entity_pills = Div(
        *[Span(_entity_label(e, lang), cls="connector-entity-pill") for e in entities],
        cls="connector-entity-row",
    )

    # ── How it works steps ────────────────────────────────────────────────────
    steps = _HOW_IT_WORKS.get(cid, [])
    steps_section = Ol(
        *[Li(t(s, lang), cls="connector-step") for s in steps],
        cls="connector-steps",
    ) if steps and not connected else Span()

    # ── Connected details ─────────────────────────────────────────────────────
    connected_details = Span()
    if connected:
        sync_info = _last_sync_info(last_run, attention, href=f"/settings/connectors/{cid}")
        dir_row = _direction_toggle(cid, config.direction if config else "both", lang)
        freq_row = _frequency_select(cid, frequency, lang) if category == ConnectorCategory.ACCOUNTING.value else Span()
        connected_details = Div(
            Div(
                Span(t("connectors.last_sync", lang) + ": ", cls="connector-meta-label"),
                Div(sync_info, id=f"connector-sync-info-{cid}", cls="connector-meta-value"),
                cls="connector-meta-row",
            ),
            dir_row,
            freq_row,
            cls="connector-connected-details",
        )

    # ── Action area ───────────────────────────────────────────────────────────
    if coming_soon:
        action_area = Div(
            Span(t("connectors.coming_soon", lang), cls="connector-badge connector-badge--soon"),
            cls="connector-action-area",
        )
    elif connected:
        action_area = Div(
            A(
                t("connectors.open", lang),
                href=f"/settings/connectors/{cid}",
                cls="btn btn--sm btn--primary",
            ),
            Button(
                t("connectors.disconnect", lang),
                cls="btn btn--sm btn--outline btn--danger",
                style="margin-left:8px;",
                hx_delete=f"/settings/connectors/{cid}/disconnect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                hx_confirm=_disconnect_confirm(cid, lang),
            ),
            cls="connector-action-area",
        )
    elif ownership == "unassigned":
        action_area = Div(
            P(
                t("connectors.unassigned" if c.get("can_reset") else "connectors.unassigned_ask_owner", lang),
                cls="flash flash--warning",
            ),
            *([Button(
                t("connectors.reset", lang),
                cls="btn btn--sm btn--outline btn--danger",
                hx_delete=f"/settings/connectors/{cid}/reset",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                hx_confirm=t("connectors.reset_confirm", lang),
            )] if c.get("can_reset") else []),
            cls="connector-action-area",
        )
    elif ambiguous:
        action_area = Div(
            P(t("connectors.ambiguous", lang), cls="flash flash--warning"),
            *([Button(
                t("connectors.disconnect", lang),
                cls="btn btn--sm btn--outline btn--danger",
                hx_delete=f"/settings/connectors/{cid}/disconnect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                hx_confirm=_disconnect_confirm(cid, lang),
            )] if c.get("local_claim") else []),
            cls="connector-action-area",
        )
    elif c.get("auth_type") == "oauth":
        # Shopify needs the shop domain on every authorize; the other OAuth
        # connectors authorize from the button alone.
        connect_label = t("connectors.finish", lang) if pending else t("connectors.connect", lang)
        if cid == "shopify":
            connect = Form(
                Input(
                    name="shop",
                    placeholder=t("connectors.shop_url_placeholder", lang),
                    cls="input input--sm",
                    style="flex:1;min-width:220px;",
                ),
                Button(
                    connect_label,
                    type="submit",
                    cls="btn btn--sm btn--primary",
                    style="margin-left:8px;white-space:nowrap;",
                ),
                hx_post=f"/settings/connectors/{cid}/oauth-redirect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;gap:4px;",
            )
        else:
            connect = Button(
                connect_label,
                cls="btn btn--sm btn--primary",
                hx_post=f"/settings/connectors/{cid}/oauth-redirect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
            )
        if pending:
            action_area = Div(
                P(t("connectors.pending_hint", lang, name=name), cls="flash flash--info"),
                connect,
                Button(
                    t("btn.cancel", lang),
                    cls="btn btn--sm btn--outline",
                    style="margin-left:8px;",
                    hx_delete=f"/settings/connectors/{cid}/disconnect",
                    hx_target=f"#connector-card-{cid}",
                    hx_swap="outerHTML",
                ),
                cls="connector-action-area",
            )
        else:
            action_area = Div(
                *([P(t("connectors.other_company", lang), cls="settings-hint")]
                  if ownership == "other" else []),
                connect,
                cls="connector-action-area",
            )
    else:
        # API key auth (WooCommerce)
        disconnect = (
            Button(
                t("connectors.disconnect", lang),
                cls="btn btn--sm btn--outline btn--danger",
                hx_delete=f"/settings/connectors/{cid}/disconnect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                hx_confirm=_disconnect_confirm(cid, lang),
            )
            if config is not None else None
        )
        if cid == "celerp":
            fields = [secret_input("pairing_link", lang=lang, cls="input input--sm",
                                   placeholder=t("accountant_access.pairing_link", lang))]
        else:
            fields = [
                Input(name="store_url", placeholder="https://mystore.com",
                      cls="input input--sm", style="flex:1;min-width:200px;"),
                Input(name="consumer_key", placeholder=t("connectors.consumer_key_placeholder", lang),
                      cls="input input--sm", style="width:200px;"),
                Input(name="consumer_secret", placeholder=t("connectors.consumer_secret_placeholder", lang),
                      type="password", cls="input input--sm", style="width:160px;"),
            ]
        action_area = Div(
            *([P(t("accountant_access.connector_source_help", lang), cls="settings-hint")]
              if cid == "celerp" else []),
            Form(
                *fields,
                Button(t("connectors.connect", lang),
                       type="submit", cls="btn btn--sm btn--primary"),
                hx_post=f"/settings/connectors/{cid}/connect-apikey",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;",
            ),
            Div(id=f"connector-connect-result-{cid}"),
            *([disconnect] if disconnect is not None else []),
            cls="connector-action-area",
        )

    # ── Card assembly ─────────────────────────────────────────────────────────
    card_cls = "connector-card"
    if coming_soon:
        card_cls += " connector-card--coming-soon"
    if connected:
        card_cls += " connector-card--connected"

    return Div(
        # Header row: icon + name + status
        Div(
            Span(icon, cls="connector-icon"),
            Div(
                Div(
                    Strong(name, cls="connector-name"),
                    status_badge,
                    cls="connector-name-row",
                ),
                P(description, cls="connector-desc"),
                cls="connector-info",
            ),
            cls="connector-header",
        ),
        # Entity pills
        entity_pills,
        # How it works (pre-connect only)
        steps_section,
        # Connected details (post-connect)
        connected_details,
        # Action area
        action_area,
        *([P(A(t("connectors.send_to_accountant", lang), href=ACCOUNTANTS_URL,
               target="_blank", rel="noopener"), cls="settings-hint")]
          if cid == "celerp" else []),
        id=f"connector-card-{cid}",
        cls=card_cls,
    )


def _entitlement_cta(lang: str = "en") -> FT:
    """Shown when connecting is blocked by no active/trialing subscription - the trial
    paywall moment. A clear CTA to start the trial, not a raw error. Links straight to
    the commercial handoff resolved for this install (same policy used on the
    status/plans pages) rather than back to /settings/cloud, which would cost the user
    an extra click to find the actual subscribe button."""
    from ui.components.cloud_gate import commercial_cta
    href, label = commercial_cta("subscribe", "cloud", t("connectors.start_trial", lang), lang)
    return Div(
        P(t("connectors.no_subscription", lang), cls="settings-hint"),
        A(label, href=href, target="_blank", cls="btn btn--sm btn--primary"),
        cls="flash flash--warning connector-entitlement-cta",
    )


def _connect_refused(platform: str, reason: str | FT) -> HTMLResponse:
    """A refused connect answers beside the form, so the values typed or pasted into it
    stay where they are for the user to check."""
    if isinstance(reason, str):
        reason = Span(reason, cls="flash flash--warning")
    return HTMLResponse(to_xml(reason), headers={
        "HX-Retarget": f"#connector-connect-result-{platform}", "HX-Reswap": "innerHTML"})


def _connect_required(platform: str, exc: Exception, lang: str) -> FT | None:
    """Accountant Sync's answer when this installation has no Connect session: what
    it needs and the way to get it, in place of the session gate's own advice."""
    from ui.api_client import APIError
    if platform != "celerp" or not (
        isinstance(exc, APIError) and exc.status == 401 and "Connect" in str(exc.detail)
    ):
        return None
    from ui.components.cloud_gate import commercial_cta
    href, label = commercial_cta("subscribe", "cloud", t("btn.get_connect", lang), lang)
    return Div(
        P(t("connectors.peer_needs_connect", lang), cls="settings-hint"),
        A(label, href=href, target="_blank", cls="btn btn--sm btn--primary"),
        cls="flash flash--warning connector-entitlement-cta",
    )


async def _awaiting_reconnect(company_id: str) -> list[str]:
    """Connectors this company lost when it was deactivated and has not reconnected."""
    from celerp.connectors.ownership import connectors_awaiting_reconnect
    from celerp.db import get_session_ctx

    try:
        async with get_session_ctx() as session:
            return await connectors_awaiting_reconnect(session, company_id)
    except Exception:
        return []


def _reconnect_banner(names: list[str], lang: str = "en") -> FT:
    return P(
        t("connectors.reconnect_after_deactivation", lang, names=", ".join(names)),
        cls="flash flash--warning",
    )


async def _deposit_context(token: str) -> dict:
    """Current deposit-account choice plus the active bank accounts to pick from."""
    from ui.api_client import get_bank_accounts, get_company

    company = await get_company(token)
    banks = await get_bank_accounts(token)
    return {
        "current": str(company.get("woocommerce_deposit_account") or ""),
        "bank_accounts": list(banks.get("items", [])),
    }


async def connectors_tab_content(lang: str, token: str, category: str, company_id: str) -> FT:
    """Render one connectors tab: the catalog entries of a single category
    ("website" or "accounting" - each has its own tab on the Web Access page)."""
    from ui.config import RELAY_URL

    relay_url = RELAY_URL
    catalog, fetch_err, needs_plan = await _fetch_catalog(relay_url, company_id, token=token)

    if not catalog:
        if needs_plan:
            owned_cards = []
            for config in await _owned_connector_configs(company_id):
                try:
                    entry = _local_connector_entry(config.connector)
                except KeyError:
                    continue
                if entry["category"] != category:
                    continue
                owned_cards.append(
                    _connector_card(
                        entry,
                        None,
                        relay_url,
                        company_id,
                        config=config,
                        lang=lang,
                    )
                )
            return Div(_entitlement_cta(lang), *owned_cards, cls="settings-card")
        return Div(
            P(fetch_err or t("connectors.fetch_error", lang),
              cls="flash flash--warning"),
            cls="settings-card",
        )

    catalog = [c for c in catalog if c.get("category", "website") == category]
    if not catalog:
        return Div(
            P(t("connectors.none_in_category", lang), cls="settings-hint"),
            cls="settings-card",
        )

    last_runs = await _get_last_runs(company_id)

    configs: dict[str, object] = {}
    for c in catalog:
        cfg = await _get_connector_config(company_id, c["id"])
        if cfg is not None:
            configs[c["id"]] = cfg
        elif c.get("connected"):
            c["connected"] = False

    # Auto-sync a freshly connected store that has never synced (e.g. just returned from
    # OAuth) so the merchant's data appears without a manual step - the activation moment.
    # Idempotent: run_sync's in-progress row + concurrency guard prevent re-triggering on
    # re-render, and once any run exists this branch no longer fires.
    for c in catalog:
        if c.get("connected") and last_runs.get(c["id"]) is None:
            spawn_background(_autosync_once(company_id, c["id"], token))

    # The tab label already names the category, so the cards render directly.
    attention = {
        c["id"]: _open_attention_count(await _attention(company_id, c["id"]))
        for c in catalog if c.get("connected")
    }
    cards = [
        _connector_card(c, last_runs.get(c["id"]), relay_url, company_id,
                      config=configs.get(c["id"]), lang=lang,
                      attention=attention.get(c["id"], 0))
        for c in catalog
    ]
    names = {c["id"]: c.get("name", c["id"]) for c in catalog}
    lost = [names[cid] for cid in await _awaiting_reconnect(company_id) if cid in names]

    from ui.api_client import get_company, role_from_company
    show_recipient_access = False
    if category == "accounting":
        try:
            show_recipient_access = role_from_company(await get_company(token)) == "owner"
        except Exception:
            pass
    return Div(
        *([_reconnect_banner(lost, lang)] if lost else []),
        *cards,
        *([Div(
            H3(t("accountant_access.title", lang)),
            P(t("accountant_access.description", lang)),
            A(t("accountant_access.manage", lang), href="/settings/accountant-access",
              cls="btn btn--secondary"),
            cls="settings-card",
        )] if show_recipient_access else []),
        cls="settings-card",
    )


def _pairing_card(company_id: str, name: str, lang: str) -> FT:
    """The sending company's pairing code, copied once and pasted by the receiving owner."""
    import uuid
    from celerp.services.peer_access import pairing_code
    return Div(
        P(t("accountant_access.your_code", lang)),
        Div(
            Code(pairing_code(uuid.UUID(company_id), name), cls="cell--mono",
                 id="peer-pairing-code"),
            Button(t("btn.copy", lang), type="button",
                   **{"data-copy": "#peer-pairing-code"},
                   cls="btn btn--xs btn--secondary ml-sm"),
            cls="flex-row align-center gap-sm",
        ),
        P(t("accountant_access.give_code", lang), cls="settings-hint"),
        cls="settings-card",
    )


def _from_accountant_action(item: dict, lang: str) -> FT:
    """Reverse an entry the accountant posted, or undo its reversal; each confirm names
    the date it posts on, and says so when a locked period moves it to today; an entry
    with no open date says so instead."""
    common = {"hx_target": "#from-accountant", "hx_swap": "outerHTML"}
    if item.get("status") == "void":
        return Span(t("accountant_access.from_voided", lang))
    reversal = item.get("reversal")
    target, original, day = ((reversal["id"], reversal["ts"], reversal.get("undo_on")) if reversal
                             else (item["id"], item.get("ts"), item.get("reverse_on")))
    if day is None:
        return Span(t("accountant_access.from_no_open_date", lang, date=item.get("locked_through") or "--"),
                    " ", A(t("page.period_lock", lang), href=PERIOD_LOCK_PATH))
    url = f"/settings/connectors/celerp/from-accountant/{quote(target, safe='')}/reverse"
    key = f"accountant_access.from_{'undo' if reversal else 'reverse'}_confirm{'_moved' if day != original else ''}"
    confirm = t(key, lang, memo=item["memo"] or "--", date=day, original=original)
    if reversal:
        return Span(
            Em(t("accountant_access.from_reversed_on", lang, date=reversal["ts"])), " ",
            Button(t("btn.undo", lang), cls="btn btn--xs btn--outline", hx_post=url, hx_confirm=confirm, **common))
    return Button(t("accountant_access.from_reverse", lang), cls="btn btn--xs btn--outline", hx_post=url,
                  hx_confirm=confirm, **common)


def _from_accountant(items: list[dict] | None, lang: str, error: str | None = None) -> FT:
    """The journal entries the accountant posted in these books, newest first; None when
    they could not be read."""
    from ui.components.shell import flash
    from ui.components.table import EMPTY, fmt_money
    notice = [flash(error)] if error else []
    if items is None:
        body = []
    elif not items:
        body = [P(t("accountant_access.from_empty", lang), cls="empty-state-msg")]
    else:
        body = [Table(
            Thead(Tr(Th(t("th.date", lang)), Th(t("th.memo", lang)),
                     Th(t("accountant_access.from_lines", lang)),
                     Th(t("th.total", lang), cls="cell--number"), Th(t("th.actions", lang)))),
            Tbody(*[Tr(
                Td(item.get("ts") or EMPTY), Td(item.get("memo") or EMPTY),
                Td(item.get("lines") or EMPTY),
                Td(fmt_money(item.get("total")), cls="cell--number"),
                Td(_from_accountant_action(item, lang)),
                cls="data-row",
            ) for item in items]),
            cls="data-table",
        )]
    return Div(
        H3(t("accountant_access.from_title", lang)),
        P(t("accountant_access.from_hint", lang), cls="settings-hint"),
        *notice, *body,
        id="from-accountant", cls="settings-card",
    )


def _sync_accounts_card(lang: str) -> FT:
    """Where records held for an account, and accounts sync added, are settled."""
    from ui.routes.sync_accounts import PAGE
    return Div(
        H3(t("sync_accounts.title", lang)),
        P(t("sync_accounts.card_hint", lang), cls="settings-hint"),
        A(t("sync_accounts.open", lang), href=PAGE, cls="btn btn--secondary btn--sm"),
        cls="settings-card",
    )


async def _from_accountant_section(token: str, lang: str, error: str | None = None) -> FT:
    from ui.api_client import APIError, journals_from_accountant
    try:
        items = await journals_from_accountant(token)
    except APIError as exc:
        return _from_accountant(None, lang, error or refusal_text(exc.detail))
    return _from_accountant(items, lang, error)


def _grant_form(lang: str, firm: str) -> FT:
    from celerp.config import settings
    from celerp.services.peer_access import SCOPE_ORDER
    return Form(
        Label(t("accountant_access.firm", lang)),
        Input(name="firm", maxlength="160", value=firm, cls="form-input"),
        P(t("accountant_access.firm_hint", lang), cls="form-hint"),
        Label(t("accountant_access.pairing_code", lang)),
        Input(name="pairing_code", type="text", maxlength="1024", cls="form-input"),
        Label(t("accountant_access.address", lang)),
        Input(name="address", type="url", maxlength="512",
              value=settings.celerp_public_url or "", cls="form-input"),
        P(t("accountant_access.address_hint", lang), cls="form-hint"),
        Label(t("accountant_access.label", lang)),
        Input(name="label", maxlength="128",
              placeholder=t("accountant_access.label_example", lang),
              cls="form-input"),
        Div(*(
            Label(Input(type="checkbox", name="scopes", value=scope,
                        checked=scope in ("contacts", "products")),
                  t(f"accountant_access.{scope}", lang))
            for scope in SCOPE_ORDER
        ), cls="flex-col gap-sm"),
        Button(t("accountant_access.issue", lang), type="submit",
               cls="btn btn--primary"),
        hx_post="/settings/accountant-access/grants",
        hx_target="#accountant-access-result",
        hx_swap="innerHTML",
        hx_disabled_elt="find button",  # a second click would issue a second key nobody holds
        id="accountant-access-form", cls="form-card",
    )


def _grant_status(g: dict, lang: str, now: datetime) -> FT:
    """How long a key still works, so the owner can renew it before synchronization stops."""
    from celerp.services.peer_access import RENEW_WITHIN
    if g.get("revoked"):
        return Span(t("accountant_access.revoked", lang), cls="badge badge--neutral")
    try:
        expires = datetime.fromisoformat(str(g.get("expires_at")))
    except ValueError:
        return Span("--")
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        return Span(t("accountant_access.expired", lang), cls="badge badge--expired")
    days = (expires - now).days + (1 if (expires - now).seconds else 0)
    label = (t("accountant_access.expires_in", lang, days=days) if days > 1
             else t("accountant_access.expires_in_one", lang))
    return Span(label,
                cls="badge badge--soon" if expires - now <= RENEW_WITHIN else "badge badge--active")


def _grant_actions(g: dict, lang: str) -> FT:
    if g.get("revoked"):
        return Span("--")
    path = f"/settings/accountant-access/grants/{g['id']}"
    swap = {"hx_target": "#accountant-access-grants", "hx_swap": "outerHTML"}
    return Div(
        Button(t("accountant_access.renew", lang), type="button", hx_post=f"{path}/renew",
               cls="btn btn--secondary btn--sm", **swap),
        Button(t("accountant_access.revoke", lang), type="button", hx_post=f"{path}/revoke",
               hx_confirm=t("accountant_access.revoke_confirm", lang),
               cls="btn btn--danger btn--sm", **swap),
        cls="flex-row gap-sm",
    )


def _grants_table(grants: list[dict], lang: str, now: datetime | None = None, **attrs) -> FT:
    """The receiving company's keys, newest first; Renew key and Revoke swap this table in place."""
    from ui.components.table import format_value
    when = now or datetime.now(timezone.utc)
    if not grants:
        return Div(P(t("accountant_access.none", lang)), id="accountant-access-grants", **attrs)
    rows = [
        Tr(
            Td(g.get("label") or "--"),
            Td(", ".join(t(f"accountant_access.{scope}", lang) for scope in g.get("scopes") or []) or "--",
               cls="grant-scopes"),
            Td(format_value(g.get("created_at"), "date")),
            Td(format_value(g.get("expires_at"), "date")),
            Td(_grant_status(g, lang, when)),
            Td(_grant_actions(g, lang)),
        )
        for g in sorted(grants, key=lambda g: str(g.get("created_at") or ""), reverse=True)
    ]
    return Div(
        Table(
            Thead(Tr(
                Th(t("accountant_access.label", lang)),
                Th(t("accountant_access.scopes", lang)),
                Th(t("accountant_access.issued", lang)),
                Th(t("accountant_access.expires", lang)),
                Th(t("th.status", lang)),
                Th(t("th.actions", lang)),
            )),
            Tbody(*rows),
            cls="data-table",
        ),
        P(t("accountant_access.revoke_reason", lang), cls="settings-hint"),
        id="accountant-access-grants", **attrs,
    )


async def _owner_token(request: Request):
    """The owner's token for a key action, or the response refusing it."""
    from ui import api_client as api
    from ui.security import not_permitted_redirect
    token = _token(request)
    if not token:
        return None, RedirectResponse("/login", status_code=302)
    if (denied := await _check_permission(request, "manage_integrations")):
        return None, denied
    if api.role_from_company(getattr(request.state, "auth_company", {})) != "owner":
        return None, not_permitted_redirect(request)
    return token, None


async def _grant_action(request: Request, grant_id: str, action):
    """Run Extend or Revoke and answer with the refreshed table."""
    import uuid
    from ui import api_client as api
    from starlette.responses import Response
    token, refused = await _owner_token(request)
    if refused is not None:
        return refused
    try:
        grant_id = str(uuid.UUID(grant_id))
    except ValueError:
        return Response(status_code=404)
    lang = get_lang(request)
    errors, grants = [], []
    try:
        await action(token, grant_id)
    except api.APIError as exc:
        errors.append(str(exc))
    try:
        grants = (await api.accountant_peer_grants(token)).get("items", [])
    except api.APIError as exc:
        errors.append(str(exc))
    table = _grants_table(grants, lang)
    table.children = (*(P(e, cls="flash flash--warning") for e in errors), *table.children)
    return HTMLResponse(to_xml(table))


def setup_routes(app):

    @app.get("/settings/accountant-access")
    async def accountant_access_page(request: Request):
        from ui.api_client import APIError, accountant_peer_grants, company_backup_sender, role_from_company
        from ui.components.shell import base_shell, page_header
        from ui.config import get_role
        from ui.security import company_owner, not_permitted_redirect
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        back = A(t("connectors.back_to_connectors", lang), href="/settings/cloud?tab=accounting",
                 cls="btn btn--secondary btn--sm")
        # Anyone but the owner stays here and learns whom to ask; the checks below still
        # confirm the owner from the API before anything is shown.
        if get_role(request) != "owner":
            owner = await company_owner(token, lang)
            page = await base_shell(
                page_header(t("accountant_access.title", lang), back),
                P(t("accountant_access.owner_only", lang, owner=owner), cls="flash flash--warning"),
                title=t("accountant_access.title", lang), nav_active="settings", request=request)
            return HTMLResponse(to_xml(page), status_code=403)
        if (denied := await _check_permission(request, "manage_integrations")):
            return denied
        company = getattr(request.state, "auth_company", {})
        if role_from_company(company) != "owner":
            return not_permitted_redirect(request)
        try:
            grants = (await accountant_peer_grants(token)).get("items", [])
        except APIError as exc:
            grants = []
            notice = P(str(exc), cls="flash flash--warning")
        else:
            notice = ""
        # The firm name starts as the one this installation last sent a company copy with.
        try:
            firm = (await company_backup_sender(token)).get("firm_name") or ""
        except APIError:
            firm = ""
        return await base_shell(
            page_header(t("accountant_access.title", lang), back),
            P(t("accountant_access.description", lang)),
            P(t("accountant_access.direction_warning", lang), cls="settings-hint"),
            P(t("accountant_access.source_help", lang)),
            _grant_form(lang, firm),
            Div(id="accountant-access-result"),
            notice,
            H3(t("accountant_access.active_grants", lang)),
            _grants_table(grants, lang),
            title=t("accountant_access.title", lang), nav_active="settings",
            request=request,
        )

    @app.post("/settings/accountant-access/grants")
    async def accountant_access_issue(request: Request):
        from celerp.services.peer_access import SCOPES, is_https_origin, pairing_link, parse_pairing_code
        from ui import api_client as api
        token, refused = await _owner_token(request)
        if refused is not None:
            return refused
        lang = get_lang(request)
        form = await request.form()
        address = str(form.get("address") or "").strip().rstrip("/")
        if not is_https_origin(address):
            return Span(t("accountant_access.address_invalid", lang),
                        cls="flash flash--warning")
        if not form.getlist("scopes"):
            return Span(t("accountant_access.no_scopes", lang), cls="flash flash--warning")
        firm = str(form.get("firm") or "").strip()
        if not 1 <= len(firm) <= 160:
            return Span(t("accountant_access.firm_missing", lang), cls="flash flash--warning")
        try:
            source, source_name = parse_pairing_code(form.get("pairing_code"))
            source_id = str(source)
            label = str(form.get("label") or "").strip() or source_name
            scopes = sorted(set(str(scope) for scope in form.getlist("scopes")))
            if not (1 <= len(label) <= 128 and scopes and set(scopes) <= SCOPES):
                raise ValueError("Invalid grant selection")
        except (ValueError, TypeError):
            return Span(t("accountant_access.invalid", lang),
                        cls="flash flash--warning")
        try:
            issued = await api.issue_accountant_peer_grant(token, {
                "source_company_id": source_id, "label": label, "firm": firm,
                "scopes": scopes,
            })
        except api.APIError as exc:
            return Span(str(exc), cls="flash flash--warning")
        try:
            link = pairing_link(address, issued.get("credential"))
        except (ValueError, TypeError, AttributeError):
            return Span(t("accountant_access.unavailable", lang),
                        cls="flash flash--warning")
        content = Div(
            P(t("accountant_access.copy_now", lang)),
            Div(
                Code(link, cls="cell--mono", id="issued-pairing-link"),
                Button(t("accountant_access.copy_link", lang), type="button",
                       **{"data-copy": "#issued-pairing-link"},
                       cls="btn btn--xs btn--secondary ml-sm"),
                cls="flex-row align-center gap-sm",
            ),
            P(t("accountant_access.one_time", lang)),
            cls="settings-card",
        )
        try:
            grants = (await api.accountant_peer_grants(token)).get("items", [])
        except api.APIError:
            grants = None
        refreshed = [] if grants is None else [_grants_table(grants, lang, hx_swap_oob="true")]
        return HTMLResponse(
            to_xml(Div(content, *refreshed)),
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @app.post("/settings/accountant-access/grants/{grant_id}/renew")
    async def accountant_access_renew(request: Request, grant_id: str):
        from ui import api_client as api
        return await _grant_action(request, grant_id, api.renew_accountant_peer_grant)

    @app.post("/settings/accountant-access/grants/{grant_id}/revoke")
    async def accountant_access_revoke(request: Request, grant_id: str):
        from ui import api_client as api
        return await _grant_action(request, grant_id, api.revoke_accountant_peer_grant)

    @app.post("/settings/connectors/{platform}/oauth-redirect")
    async def connector_oauth_redirect(request: Request, platform: str, shop: str = ""):
        """HTMX: get OAuth authorize URL and return JS to open it in a new tab."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        lang = get_lang(request)

        from celerp.connectors.registry import service_name
        from ui.api_client import APIError, get_connector_authorize_url
        try:
            result = await get_connector_authorize_url(token, platform, shop=shop)
        except APIError as exc:
            if exc.status == 402:  # no active/trialing subscription
                return _entitlement_cta(lang)
            return Span(exc.detail, cls="flash flash--warning")
        except Exception as exc:
            return Span(str(exc), cls="flash flash--warning")

        if "error" in result:
            return Span(result["error"], cls="flash flash--warning")

        url = result.get("authorize_url", "")
        if not url:
            return Span(t("connectors.authorize_error", lang, service=service_name(platform)), cls="flash flash--warning")

        if not is_safe_authorize_url(url):
            return Span(t("connectors.authorize_error", lang, service=service_name(platform)), cls="flash flash--warning")

        # Open the authorize URL in a new tab AND show an on-screen next step + fallback
        # link, so nothing is silently lost if the popup is blocked (GDR: users must
        # always know what comes next and have a visible way forward).
        return Div(
            Script(f"window.open({url!r}, '_blank', 'noopener');"),
            Span(t("connectors.authorize_opened", lang,
                   default="Opening authorization in a new tab, complete it there, then return here."),
                 cls="flash flash--info"),
            A(t("connectors.authorize_link", lang, default="If nothing opened, click here to authorize"),
              href=url, target="_blank", rel="noopener", cls="btn btn--sm btn--outline"),
        )

    @app.post("/settings/connectors/{platform}/frequency")
    async def connector_set_frequency(request: Request, platform: str):
        """HTMX: update sync frequency for an accounting connector."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )
        from celerp.services.company_lock import lock_company
        from celerp.db import get_session_ctx
        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)
        form = await request.form()
        frequency = form.get("sync_frequency", "manual")

        if frequency not in ("manual", "daily"):
            frequency = "manual"

        async with get_session_ctx() as session:
            authorize_in_process(session, request, "manage_integrations")
            try:
                config = await lock_connector_operation(
                    session, company_id, platform, require_owner=True
                )
                await lock_company(session, uuid.UUID(str(company_id)))
            except (ConnectorOwnershipError, HTTPException) as exc:
                await session.rollback()
                return Span(_refusal(exc), cls="flash flash--warning")
            config.sync_frequency = frequency
            await session.commit()
        if request.query_params.get("detail"):
            return _frequency_select(platform, frequency, lang, detail=True)

        catalog, _fetch_err, _needs_plan = await _fetch_catalog(RELAY_URL, company_id, token=token)
        c_data = next(
            (c for c in catalog if c["id"] == platform),
            _local_connector_entry(platform),
        )
        last_runs = await _get_last_runs(company_id)
        config = await _get_connector_config(company_id, platform)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, company_id,
                              config=config, lang=lang,
                              attention=_open_attention_count(await _attention(company_id, platform)))

    @app.post("/settings/connectors/{platform}/direction")
    async def connector_set_direction(request: Request, platform: str):
        """HTMX: update sync direction (inbound / outbound / both) for a connector."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )
        from celerp.services.company_lock import lock_company
        from celerp.db import get_session_ctx
        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)
        form = await request.form()
        direction = form.get("direction")
        if direction not in ("inbound", "outbound", "both"):
            return Span(t("connectors.invalid_direction", lang), cls="flash flash--warning")

        async with get_session_ctx() as session:
            authorize_in_process(session, request, "manage_integrations")
            try:
                config = await lock_connector_operation(
                    session, company_id, platform, require_owner=True
                )
                await lock_company(session, uuid.UUID(str(company_id)))
            except (ConnectorOwnershipError, HTTPException) as exc:
                await session.rollback()
                # Nothing is swapped, so the buttons stay; the reason is a toast.
                from starlette.responses import Response
                from ui.components.shell import toast_header
                return Response(headers={**toast_header(_refusal(exc), "error", persist=True),
                                         "HX-Reswap": "none"})
            config.direction = direction
            await session.commit()
        if request.query_params.get("detail"):
            return _direction_toggle(platform, direction, lang, detail=True)

        catalog, _fetch_err, _needs_plan = await _fetch_catalog(RELAY_URL, company_id, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(company_id)
        config = await _get_connector_config(company_id, platform)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, company_id,
                              config=config, lang=lang,
                              attention=_open_attention_count(await _attention(company_id, platform)))

    @app.delete("/settings/connectors/{platform}/disconnect")
    async def connector_disconnect(request: Request, platform: str):
        """HTMX: disconnect a connector by deleting its tokens on relay."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)

        from ui.api_client import APIError, delete_connector_credentials
        force = request.query_params.get("force") == "1"
        try:
            revoke_result = await delete_connector_credentials(token, platform, force=force)
        except APIError as exc:
            revoke_result = {"ok": False, "detail": exc.detail}
        except Exception as exc:
            revoke_result = {"ok": False, "detail": str(exc)}
        if not revoke_result.get("ok"):
            # The detail page shows the outcome under its buttons; the overview
            # replaces the connector's card.
            if request.query_params.get("redirect"):
                return _disconnect_failed(
                    revoke_result, lang, platform=platform, force=force,
                    retry=f"/settings/connectors/{platform}/disconnect?force=1&redirect=1",
                    target=f"#connector-disconnect-result-{platform}", swap="innerHTML",
                )
            return _disconnect_failed(
                revoke_result, lang, platform=platform, force=force,
                retry=f"/settings/connectors/{platform}/disconnect?force=1",
                target=f"#connector-card-{platform}", swap="outerHTML",
                id=f"connector-card-{platform}", cls="connector-card",
            )

        if request.query_params.get("redirect"):
            # Disconnected from the full-page detail view -> return to the connector's tab.
            from starlette.responses import Response
            tab = _local_connector_entry(platform)["category"]
            return Response(status_code=204, headers={"HX-Redirect": f"/settings/cloud?tab={tab}"})

        catalog, _fetch_err, _needs_plan = await _fetch_catalog(RELAY_URL, company_id, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(company_id)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, company_id, lang=lang)

    @app.delete("/settings/connectors/{platform}/reset")
    async def connector_reset(request: Request, platform: str):
        """HTMX: reset a connection no company owns, for the whole installation."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from ui.api_client import reset_unassigned_connector
        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)
        force = request.query_params.get("force") == "1"
        try:
            result = await reset_unassigned_connector(token, platform, force=force)
        except Exception as exc:
            result = {"ok": False, "detail": str(exc)}
        if not result.get("ok"):
            return _disconnect_failed(
                result, lang, platform=platform, force=force,
                retry=f"/settings/connectors/{platform}/reset?force=1",
                target=f"#connector-card-{platform}", swap="outerHTML",
                id=f"connector-card-{platform}", cls="connector-card",
            )

        catalog, _fetch_err, _needs_plan = await _fetch_catalog(RELAY_URL, company_id, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(company_id)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, company_id, lang=lang)

    @app.post("/settings/connectors/{platform}/sync")
    async def connector_sync_now(request: Request, platform: str):
        """HTMX: kick off a sync (background) and return the live status view, which polls
        itself until terminal. Setup failures (bad token, unknown connector) return inline;
        per-entity sync failures surface in the status table + the completion toast."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

    
        from ui.api_client import APIError

        company_id = _request_company_id(request)
        lang = get_lang(request)

        try:
            await _kickoff_connector_sync(company_id, platform, token)
        except APIError as exc:
            return (_connect_required(platform, exc, lang)
                    or Span(f"✗ {refusal_text(exc.detail) or exc.detail}", cls="flash flash--warning"))
        except Exception as exc:
            return Span(f"✗ {exc}", cls="flash flash--warning")

        # Force the first poll so the view picks up the in-progress rows the background
        # task is writing.
        runs = await _entity_runs(company_id, platform)
        return _connector_status_view(platform, runs, lang, force_poll=True,
                                      attention=await _attention(company_id, platform))

    @app.get("/settings/connectors/{platform}/status")
    async def connector_status(request: Request, platform: str):
        """Live per-entity status fragment. Self-re-triggers while in progress; on the poll
        that first sees all-terminal it stops polling and fires a one-time completion toast
        (only when ?polling=1, so opening the detail page never fires a stale toast)."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.connectors.registry import service_name
        company_id = _request_company_id(request)
        lang = get_lang(request)
        runs = await _entity_runs(company_id, platform)
        attention = await _attention(company_id, platform)
        view = _connector_status_view(platform, runs, lang, attention=attention)
        polling = request.query_params.get("polling") == "1"
        if polling and runs and not _any_in_progress(runs):
            msg, kind = _sync_complete_toast(runs, attention, service_name(platform), lang)
            from ui.components.shell import toast_header
            return HTMLResponse(to_xml(view), headers=toast_header(msg, kind))
        return view

    @app.post("/settings/connectors/{platform}/deposit-account")
    async def connector_set_deposit_account(request: Request, platform: str):
        """HTMX: choose the account payments from this store's orders are booked to."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if platform != "woocommerce":
            return Span(t("connectors.unknown_connector", platform=platform), cls="flash flash--warning")

        from starlette.responses import Response
        from ui.api_client import APIError, patch_company
        from ui.components.shell import flash

        lang = get_lang(request)
        form = await request.form()
        code = str(form.get("woocommerce_deposit_account", "")).strip()
        if code == "__new__":
            return Response(status_code=204, headers={"HX-Redirect": "/settings/accounting/bank-accounts/new"})

        deposit = await _deposit_context(token)
        known = {ba.get("chart_account_code") for ba in deposit["bank_accounts"]}
        if code and code not in known:
            return Div(
                flash(t("connectors.deposit_unknown_account", lang, code=code)),
                _deposit_select_row(platform, deposit["current"], deposit["bank_accounts"], lang),
                id=f"connector-deposit-{platform}",
            )
        try:
            await patch_company(token, {"woocommerce_deposit_account": code})
        except APIError as exc:
            return Div(
                flash(str(exc.detail)),
                _deposit_select_row(platform, deposit["current"], deposit["bank_accounts"], lang),
                id=f"connector-deposit-{platform}",
            )
        return Div(
            flash(t("flash.saved", lang), "success"),
            _deposit_select_row(platform, code, deposit["bank_accounts"], lang),
            id=f"connector-deposit-{platform}",
        )

    async def _set_reconciled(request: Request, platform: str, record_id: str, mark: bool):
        """HTMX: mark or unmark one attention entry as reconciled by hand, then
        re-render it. Only WooCommerce orders carry this action."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if platform != "woocommerce":
            return Span(t("connectors.unknown_connector", platform=platform), cls="flash flash--warning")

        from ui.api_client import APIError, set_woocommerce_order_reconciled

        lang = get_lang(request)
        signature = str((await request.form()).get("signature", "")).strip() if mark else None
        try:
            result = await set_woocommerce_order_reconciled(token, record_id, signature)
        except APIError as exc:
            entries = await _attention(_request_company_id(request), platform)
            entry = next((e for e in entries if str(e.get("id")) == record_id), {"id": record_id})
            return _attention_item(platform, entry, lang, error=str(exc.detail))
        return _attention_item(platform, result["entry"], lang)

    @app.post("/settings/connectors/{platform}/attention/{record_id}/reconciled")
    async def connector_mark_reconciled(request: Request, platform: str, record_id: str):
        return await _set_reconciled(request, platform, record_id, mark=True)

    @app.delete("/settings/connectors/{platform}/attention/{record_id}/reconciled")
    async def connector_unmark_reconciled(request: Request, platform: str, record_id: str):
        return await _set_reconciled(request, platform, record_id, mark=False)

    @app.post("/settings/connectors/celerp/from-accountant/{je_id}/reverse")
    async def from_accountant_reverse(request: Request, je_id: str):
        """HTMX: reverse an entry the accountant posted, or undo its reversal, then show
        the section again with what happened."""
        from ui.api_client import APIError, reverse_from_accountant
        from ui.components.shell import toast_header

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        lang = get_lang(request)
        try:
            result = await reverse_from_accountant(token, je_id)
        except APIError as exc:
            return await _from_accountant_section(token, lang, refusal_text(exc.detail))
        section = await _from_accountant_section(token, lang)
        date = result.get("ts") or "--"
        if result.get("already"):  # a stale tab repeated it; nothing new was posted
            return HTMLResponse(to_xml(section), headers=toast_header(
                t("accountant_access.from_already", lang, date=date), "info"))
        return HTMLResponse(to_xml(section), headers=toast_header(
            t("accountant_access.from_posted", lang, date=date), "success"))

    @app.get("/settings/connectors/{platform}")
    async def connector_detail_page(request: Request, platform: str):
        """Full-page connector detail (status + config + actions). Reached from the overview
        'Open' link; the back-link returns to the connectors settings tab."""
        from starlette.responses import RedirectResponse

        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if _validate_platform(platform) is not None:
            return RedirectResponse("/settings/cloud?tab=website", status_code=302)

        from ui.components.shell import base_shell, page_header
        from ui.components.table import breadcrumbs
        from ui.config import RELAY_URL

        company_id = _request_company_id(request)
        lang = get_lang(request)
        catalog, _fetch_err, _needs_plan = await _fetch_catalog(RELAY_URL, company_id, token=token)
        c = next((x for x in catalog if x["id"] == platform), _local_connector_entry(platform))
        config = await _get_connector_config(company_id, platform)
        runs = await _entity_runs(company_id, platform)
        deposit = await _deposit_context(token) if platform == "woocommerce" else None
        icon = _CONNECTOR_ICONS.get(platform, "🔌")
        name = c.get("name", platform.title())
        return await base_shell(
            breadcrumbs([(t("settings.tab_connectors", lang), f"/settings/cloud?tab={c.get('category', 'website')}"), (name, None)]),
            page_header(
                f"{icon} {name}",
                A(t("connectors.back_to_connectors", lang), href=f"/settings/cloud?tab={c.get('category', 'website')}",
                  cls="btn btn--secondary btn--sm"),
            ),
            _pairing_card(company_id, str(getattr(request.state, "auth_company", {}).get("name") or ""), lang)
            if platform == "celerp" else "",
            _connector_detail_body(c, runs, config, lang, deposit=deposit,
                                   attention=await _attention(company_id, platform)),
            await _from_accountant_section(token, lang) if platform == "celerp" else "",
            _sync_accounts_card(lang) if platform == "celerp" else "",
            title=f"{name} - Celerp",
            nav_active="settings",
            request=request,
        )

    @app.post("/settings/connectors/{platform}/connect-apikey")
    async def connector_connect_apikey(request: Request, platform: str):
        """HTMX: save API key credentials for a connector (e.g. WooCommerce)."""
        token = _token(request)
        if not token:
            return Span(t("error.session_expired"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)
        form = await request.form()
        # Canonicalise (no trailing slash) so the stored handle matches the store
        # URL WooCommerce sends in webhook deliveries (X-WC-Webhook-Source).
        store_url = form.get("store_url", "").strip().rstrip("/")
        consumer_key = form.get("consumer_key", "").strip()
        consumer_secret = form.get("consumer_secret", "").strip()

        if platform not in ("woocommerce", "celerp"):
            return Span(t("connectors.unknown_connector", lang, platform=platform))
        if platform == "celerp":
            from celerp.services.peer_access import parse_pairing_link
            try:
                store_url, consumer_key = parse_pairing_link(form.get("pairing_link"))
            except ValueError:
                return _connect_refused(platform, t("accountant_access.pairing_link_invalid", lang))
            store_url = store_url.rstrip("/")
        if not consumer_key or (platform == "woocommerce" and not consumer_secret):
            return _connect_refused(platform, t("connectors.missing_credentials", lang))

        if (url_err := _store_url_error(store_url, platform)) is not None:
            return _connect_refused(platform, t(url_err, lang))

        # Validation + storage run in the API process, which holds the live relay
        # session (the UI process has none). The proxy probes the store first, so a
        # bad key/secret/URL fails here with a clear message instead of silently
        # failing on the first background sync.
        from ui.api_client import delete_connector_credentials, store_connector_credentials
        try:
            result = await store_connector_credentials(
                token, platform, consumer_key, consumer_secret, store_url)
        except Exception as exc:
            return _connect_refused(platform, _connect_required(platform, exc, lang) or f"✗ {exc}")

        if not result.get("ok"):
            # Surface a failed store rather than silently rendering "connected".
            err = result.get("error", "")
            detail = refusal_text(result.get("detail"))
            if err == "subscription_required":
                msg = t("connectors.no_subscription", lang)
            elif err in ("store_rejected", "store_unreachable"):
                msg = t("connectors.connect_check_failed", lang, detail=detail.rstrip("."))
            elif err in ("already_connected", "store_changed") and detail:
                msg = detail
            else:
                msg = t("connectors.connect_failed", lang)
            return _connect_refused(platform, msg)

        # Create connector config with defaults
        catalog, _fetch_err, _needs_plan = await _fetch_catalog(RELAY_URL, company_id, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        config = await _get_connector_config(company_id, platform)
        if config is None:
            return Div(
                Span(
                    t("connectors.connect_failed", lang),
                    cls="flash flash--warning",
                ),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        # Auto-sync on connect so the merchant's data appears without a manual step
        # (the activation moment). Best-effort: a failure here doesn't block the connect.
        try:
            await _kickoff_connector_sync(company_id, platform, token)
        except Exception:
            log.warning("auto-sync on connect failed (non-fatal) for %s", platform, exc_info=True)

        last_runs = await _get_last_runs(company_id)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, company_id,
                              config=config, lang=lang,
                              attention=_open_attention_count(await _attention(company_id, platform)))
