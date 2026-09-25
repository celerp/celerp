# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Web Access: Connectors tab."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import HTMLResponse

from ui.components.attrs import hx_vals
from ui.components.activity import relative_time
from ui.i18n import t, get_lang
from ui.routes.settings import _check_permission, _token
from ui.security import is_safe_authorize_url

from celerp.connectors.base import ConnectorCategory, SyncFrequency
from celerp.connectors.sync_runner import CONNECTOR_RESET_ENTITY, attention_entries
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
    elif platform == "woocommerce":
        return "connectors.store_url_required"
    return None

_CONNECTOR_ICONS: dict[str, str] = {
    "shopify": "🛍️",
    "woocommerce": "🛒",
    "quickbooks": "📊",
    "xero": "📗",
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


def _last_sync_info(run, attention: int = 0) -> FT:
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
    counts = f"+{run.created_count:,} ~{run.updated_count:,}"
    parts = [relative_time(finished.isoformat()), _sync_status_label(run.status), counts]
    if attention:
        parts.append(t("connectors.attention_count", n=attention))
    return Span(" · ".join(parts), cls=f"connector-sync-info {status_cls}")


def _direction_toggle(cid: str, current: str, lang: str = "en") -> FT:
    """Three-option sync direction toggle: Inbound / Outbound / Both."""
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
                hx_post=f"/settings/connectors/{cid}/direction",
                hx_vals=hx_vals({"direction": val}),
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
            )
        )
    return Div(
        Span(t("connectors.direction", lang, default="Direction") + ": ", cls="connector-label"),
        *buttons,
        cls="connector-direction-toggle",
    )


def _frequency_select(cid: str, current: str, lang: str = "en") -> FT:
    """Sync frequency selector for accounting connectors."""
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
            hx_post=f"/settings/connectors/{cid}/frequency",
            hx_target=f"#connector-card-{cid}",
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
    try:
        return await attention_entries(company_id, platform)
    except Exception:
        log.warning("could not read the attention list for %s", platform, exc_info=True)
        return []


def _open_attention_count(entries: list[dict]) -> int:
    """Entries not yet marked reconciled."""
    return sum(1 for e in entries if not e.get("reconciled"))


def _request_company_id(request: Request) -> str:
    """Return the API-verified company id cached by the permission gate."""
    company = getattr(request.state, "auth_company", None)
    company_id = company.get("id") if isinstance(company, dict) else None
    if not company_id:
        raise RuntimeError("Current company is unavailable")
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
        "auth_type": "api_key" if platform == "woocommerce" else "oauth",
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
        raise RuntimeError(result.get("detail") or "Could not start connector sync")


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
    "invoices": "nav.invoices",
    "products_out": "connectors.entity_products_out",
    "invoices_out": "connectors.entity_invoices_out",
    "inventory_out": "connectors.entity_inventory_out",
}


def _entity_label(entity: str, lang: str = "en") -> str:
    """Translated display label for a sync entity type, resolved at render time.
    Unknown entities degrade honestly to their raw value."""
    key = _ENTITY_LABELS.get(entity)
    return t(key, lang) if key else entity

_ENTITY_ORDER = ["products", "orders", "contacts", "inventory", "invoices",
                 "products_out", "invoices_out", "inventory_out"]


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


def _status_badge_for(status: str, lang: str = "en") -> FT:
    cls = {"success": "badge--active", "partial": "badge--draft",
           "failed": "badge--overdue", "running": "badge--inactive"}.get(status, "badge--inactive")
    label = {"success": t("connectors.status_ok", lang), "partial": t("connectors.status_partial", lang),
             "failed": t("connectors.status_failed", lang), "running": t("connectors.syncing", lang)}.get(
        status, status or "-")
    return Span(label, cls=f"badge {cls}")


def _entity_status_table(runs: dict, lang: str = "en") -> FT:
    """Per-entity sync status table. Shared by the detail view and the polling fragment."""
    present = [e for e in _ENTITY_ORDER if e in runs]
    if not present:
        return P(t("connectors.never_synced", lang), cls="empty-state-msg")
    rows = []
    for e in present:
        r = runs[e]
        running = getattr(r, "finished_at", None) is None
        when = t("connectors.syncing", lang) if running else relative_time(r.finished_at.isoformat())
        counts = "…" if running else f"+{r.created_count:,} ~{r.updated_count:,}"
        errs = list(getattr(r, "errors", None) or [])
        rows.append(Tr(
            Td(_entity_label(e, lang)),
            Td(when),
            Td(_status_badge_for("running" if running else r.status, lang)),
            Td(counts, cls="cell--number"),
            Td(str(len(errs)) if errs else "--", cls="cell--number", title="; ".join(errs) if errs else ""),
            cls="data-row",
        ))
    return Table(
        Thead(Tr(Th(t("connectors.entity", lang)), Th(t("connectors.last_sync", lang)),
                 Th(t("connectors.status", lang)), Th(t("connectors.changes", lang)),
                 Th(t("connectors.issues_header", lang)))),
        Tbody(*rows),
        cls="data-table",
    )


def _attention_item(platform: str, entry: dict, lang: str = "en", error: str | None = None) -> FT:
    """One record waiting on a person. A change only a person can reconcile
    (it carries a signature) offers Mark reconciled, and Undo once marked.
    ``error`` explains why the last Mark or Undo did not apply."""
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
    return Li(
        Strong(str(entry.get("label", record_id))), ": ", str(entry.get("reason", "")),
        action, flash(error) if error else Span(), id=item_id,
    )


def _attention_list(platform: str, entries: list[dict], lang: str = "en") -> FT:
    """Records the connector could not import and is holding for a person (an
    order whose customer or product is missing, or a refund to reconcile). The
    list is kept until a sync produces a new one, so a failed run never hides it.
    Orders a person marked reconciled sit in a collapsed section under the open
    ones, with Undo, until the order changes in the store."""
    if not entries:
        return Span()
    open_entries = [e for e in entries if not e.get("reconciled")]
    reconciled = [e for e in entries if e.get("reconciled")]
    return Div(
        P(t("connectors.attention_header", lang), cls="settings-section-title"),
        Ul(*[_attention_item(platform, e, lang) for e in open_entries]) if open_entries else Span(),
        Details(
            Summary(t("connectors.attention_reconciled_header", lang, n=len(reconciled)),
                    cls="text-muted mt-md"),
            Ul(*[_attention_item(platform, e, lang) for e in reconciled]),
        ) if reconciled else Span(),
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
    polling = force_poll or _any_in_progress(runs)
    attrs: dict = {}
    if polling:
        attrs = {"hx_get": f"/settings/connectors/{platform}/status?polling=1",
                 "hx_trigger": "load delay:2s", "hx_swap": "outerHTML"}
    return Div(
        _entity_status_table(runs, lang),
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
        hx_confirm=t("connectors.disconnect_confirm", lang),
        hx_swap="none",
    )
    config_rows = [_direction_toggle(cid, config.direction if config else "both", lang)]
    if category == ConnectorCategory.ACCOUNTING.value:
        config_rows.append(_frequency_select(cid, frequency, lang))
    if deposit is not None:
        config_rows.append(_deposit_select_row(cid, deposit["current"], deposit["bank_accounts"], lang))

    return Div(
        Div(sync_btn, disconnect_btn, cls="connector-action-area"),
        Div(*config_rows, cls="connector-connected-details"),
        P(t("connectors.status", lang), cls="settings-section-title"),
        _connector_status_view(cid, runs, lang, attention=attention),
        P(A(t("connectors.view_synced"), href=f"/inventory?source={cid}",
            cls="btn btn--sm btn--outline"), cls="connector-synced-link"),
        cls="settings-card",
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
        sync_info = _last_sync_info(last_run, attention)
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
                hx_confirm=t("connectors.disconnect_confirm", lang),
            ),
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
                hx_confirm=t("connectors.disconnect_confirm", lang),
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
                hx_get=f"/settings/connectors/{cid}/oauth-redirect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;gap:4px;",
            )
        else:
            connect = Button(
                connect_label,
                cls="btn btn--sm btn--primary",
                hx_get=f"/settings/connectors/{cid}/oauth-redirect",
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
                hx_confirm=t("connectors.disconnect_confirm", lang),
            )
            if config is not None else None
        )
        action_area = Div(
            Form(
                Input(name="store_url", placeholder="https://mystore.com",
                      cls="input input--sm", style="flex:1;min-width:200px;"),
                Input(name="consumer_key", placeholder=t("connectors.consumer_key_placeholder", lang),
                      cls="input input--sm", style="width:160px;"),
                Input(name="consumer_secret", placeholder=t("connectors.consumer_secret_placeholder", lang),
                      type="password", cls="input input--sm", style="width:160px;"),
                Button(t("connectors.connect", lang),
                       type="submit", cls="btn btn--sm btn--primary"),
                hx_post=f"/settings/connectors/{cid}/connect-apikey",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;",
            ),
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
            P(fetch_err or t("connectors.fetch_error", lang,
                default="Could not load connectors from relay. Check your connection."),
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

    return Div(
        *([_reconnect_banner(lost, lang)] if lost else []),
        *cards,
        cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/connectors/{platform}/oauth-redirect")
    async def connector_oauth_redirect(request: Request, platform: str, shop: str = ""):
        """HTMX: get OAuth authorize URL and return JS to open it in a new tab."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        lang = get_lang(request)

        from ui.api_client import APIError, get_connector_authorize_url
        try:
            result = await get_connector_authorize_url(token, platform, shop=shop)
        except APIError as exc:
            if exc.status == 402:  # no active/trialing subscription
                return _entitlement_cta(lang)
            return Span(str(exc), cls="flash flash--warning")
        except Exception as exc:
            return Span(str(exc), cls="flash flash--warning")

        if "error" in result:
            return Span(result["error"], cls="flash flash--warning")

        url = result.get("authorize_url", "")
        if not url:
            return Span(t("connectors.authorize_error", lang), cls="flash flash--warning")

        if not is_safe_authorize_url(url):
            return Span(t("connectors.authorize_error", lang), cls="flash flash--warning")

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
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )
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
            try:
                config = await lock_connector_operation(
                    session, company_id, platform, require_owner=True
                )
            except ConnectorOwnershipError as exc:
                await session.rollback()
                return Span(str(exc), cls="flash flash--warning")
            config.sync_frequency = frequency
            await session.commit()

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
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.connectors.ownership import (
            ConnectorOwnershipError,
            lock_connector_operation,
        )
        from celerp.db import get_session_ctx
        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)
        form = await request.form()
        direction = form.get("direction", "both")
        if direction not in ("inbound", "outbound", "both"):
            direction = "both"

        async with get_session_ctx() as session:
            try:
                config = await lock_connector_operation(
                    session, company_id, platform, require_owner=True
                )
            except ConnectorOwnershipError as exc:
                await session.rollback()
                return Span(str(exc), cls="flash flash--warning")
            config.direction = direction
            await session.commit()

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
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from ui.config import RELAY_URL
        from ui.i18n import get_lang

        company_id = _request_company_id(request)
        lang = get_lang(request)

        from ui.api_client import delete_connector_credentials
        try:
            revoke_result = await delete_connector_credentials(token, platform)
        except Exception as exc:
            revoke_result = {"ok": False, "detail": str(exc)}
        if not revoke_result.get("ok"):
            detail = revoke_result.get("detail") or revoke_result.get("error") or "disconnect_failed"
            return Div(
                Span(
                    t(
                        "connectors.disconnect_failed",
                        lang,
                        detail=detail,
                        default="Disconnect failed; nothing was changed: {detail}",
                    ),
                    cls="flash flash--warning",
                ),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        if request.query_params.get("redirect"):
            # Disconnected from the full-page detail view -> return to the overview tab.
            from starlette.responses import Response
            return Response(status_code=204, headers={"HX-Redirect": "/settings/cloud?tab=website"})

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
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

    
        company_id = _request_company_id(request)
        lang = get_lang(request)

        try:
            await _kickoff_connector_sync(company_id, platform, token)
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
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := await _check_permission(request, "manage_integrations")):
            return r
        if (err := _validate_platform(platform)):
            return err

    
        company_id = _request_company_id(request)
        lang = get_lang(request)
        runs = await _entity_runs(company_id, platform)
        view = _connector_status_view(platform, runs, lang,
                                      attention=await _attention(company_id, platform))
        polling = request.query_params.get("polling") == "1"
        if polling and runs and not _any_in_progress(runs):
            ok = _overall_status(runs) != "failed"
            msg = t("connectors.sync_complete", lang) if ok else t("connectors.sync_failed", lang)
            from ui.components.shell import toast_header
            return HTMLResponse(
                to_xml(view),
                headers=toast_header(msg, "success" if ok else "error"),
            )
        return view

    @app.post("/settings/connectors/{platform}/deposit-account")
    async def connector_set_deposit_account(request: Request, platform: str):
        """HTMX: choose the account payments from this store's orders are booked to."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
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
            return Span(t("error.unauthorized"), cls="flash flash--warning")
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
        c = next((x for x in catalog if x["id"] == platform),
                 {"id": platform, "name": platform.title(), "category": "website"})
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
            _connector_detail_body(c, runs, config, lang, deposit=deposit,
                                   attention=await _attention(company_id, platform)),
            title=f"{name} - Celerp",
            nav_active="settings",
            request=request,
        )

    @app.post("/settings/connectors/{platform}/connect-apikey")
    async def connector_connect_apikey(request: Request, platform: str):
        """HTMX: save API key credentials for a connector (e.g. WooCommerce)."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
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

        if not consumer_key or not consumer_secret:
            return Div(
                Span(t("connectors.missing_credentials", lang, default="Consumer key and secret are required."),
                     cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        if (url_err := _store_url_error(store_url, platform)) is not None:
            _defaults = {
                "connectors.store_url_must_use_https": "Store URL must use https:// (API keys are sent as Basic Auth).",
                "connectors.store_url_required": "Store URL is required.",
            }
            return Div(
                Span(t(url_err, lang, default=_defaults[url_err]), cls="flash flash--warning"),
                id=f"connector-card-{platform}", cls="connector-card",
            )

        # Validation + storage run in the API process, which holds the live relay
        # session (the UI process has none). The proxy probes the store first, so a
        # bad key/secret/URL fails here with a clear message instead of silently
        # failing on the first background sync.
        from ui.api_client import delete_connector_credentials, store_connector_credentials
        try:
            result = await store_connector_credentials(
                token, platform, consumer_key, consumer_secret, store_url)
        except Exception as exc:
            return Div(
                Span(f"✗ {exc}", cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        if not result.get("ok"):
            # Surface a failed store rather than silently rendering "connected".
            err = result.get("error", "")
            detail = result.get("detail", "")
            if err == "subscription_required":
                msg = t("connectors.no_subscription", lang)
            elif err in ("store_rejected", "store_unreachable"):
                msg = t("connectors.connect_check_failed", lang, detail=detail)
            elif err in ("already_connected", "store_changed") and detail:
                msg = detail
            else:
                msg = t("connectors.connect_failed", lang)
            return Div(
                Span(msg, cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

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
