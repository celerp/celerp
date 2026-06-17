# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Settings - Web Access: Connectors tab."""

from __future__ import annotations

from datetime import datetime, timezone

from fasthtml.common import *
from starlette.requests import Request

from ui.i18n import t, get_lang
from ui.routes.settings import _check_role, _token

from celerp.connectors.base import ConnectorCategory, SyncDirection, SyncFrequency

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
        return Span(f"Unknown connector: {platform}", cls="flash flash--warning")
    return None


def _entity_chip(entity: str) -> FT:
    return Span(entity, cls="connector-entity-chip")


def _status_badge(connected: bool, lang: str = "en") -> FT:
    if connected:
        return Span("✓ " + t("connectors.connected", lang, default="Connected"),
                    cls="badge badge--active")
    return Span(t("connectors.not_connected", lang, default="Not Connected"),
                cls="badge badge--inactive")


def _coming_soon_badge(lang: str = "en") -> FT:
    return Span(t("connectors.coming_soon", lang, default="Coming Soon"),
                cls="badge badge--coming-soon")


def _last_sync_info(run) -> FT:
    """Render last SyncRun info or '-'."""
    if run is None:
        return Span("-", cls="connector-sync-info")
    finished = run.finished_at
    if finished:
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - finished
        minutes = int(delta.total_seconds() // 60)
        if minutes < 60:
            time_str = f"{minutes}m ago"
        elif minutes < 1440:
            time_str = f"{minutes // 60}h ago"
        else:
            time_str = f"{minutes // 1440}d ago"
    else:
        time_str = "in progress"

    status_cls = {
        "success": "connector-sync-info--ok",
        "partial": "connector-sync-info--warn",
        "failed": "connector-sync-info--err",
    }.get(run.status, "")

    counts = f"+{run.created_count} ~{run.updated_count}"
    return Span(
        f"{time_str} · {run.status} · {counts}",
        cls=f"connector-sync-info {status_cls}",
    )


def _direction_toggle(cid: str, current: str, lang: str = "en") -> FT:
    """Three-option direction toggle: Inbound / Outbound / Both."""
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
                hx_vals=f'{{"direction": "{val}"}}',
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


def _webhook_status(connected: bool, category: str, lang: str = "en") -> FT:
    """Show webhook live status for e-commerce connectors."""
    if category != ConnectorCategory.WEBSITE.value:
        return Span()
    if connected:
        return Span(
            "● " + t("connectors.live", lang, default="Live"),
            cls="connector-webhook-status connector-webhook-status--live",
        )
    return Span()


async def _fetch_access_token(relay_url: str, instance_id: str, platform: str) -> dict:
    """Fetch decrypted access token from relay for a platform."""
    import os
    import httpx
    if not relay_url.startswith("https://"):
        if not os.environ.get("CELERP_ALLOW_HTTP_RELAY"):
            raise RuntimeError("Relay URL must use HTTPS. Set CELERP_ALLOW_HTTP_RELAY=1 for development.")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                f"{relay_url}/tokens/{platform}/access-token",
                params={"instance_id": instance_id},
            )
            if r.status_code == 404:
                raise RuntimeError(f"No {platform} connection found. Complete the OAuth flow first.")
            if r.status_code == 401:
                raise RuntimeError(f"{platform} token expired. Please reconnect.")
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"Relay returned {exc.response.status_code}: {exc.response.text}") from exc
    except httpx.ConnectError:
        raise RuntimeError("Cannot reach relay server. Check your internet connection.")


async def _fetch_catalog(relay_url: str, instance_id: str, token: str = "") -> tuple[list[dict], str]:
    """Fetch connector catalog via API process proxy (which holds the gateway token).
    Returns (connectors, error_detail) - error_detail is "" on success."""
    from ui.api_client import get_connectors_catalog
    try:
        return await get_connectors_catalog(token)
    except Exception as exc:
        return [], str(exc)


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
                .where(SyncRun.company_id == company_id)
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


async def _get_connector_config(company_id: str, connector: str):
    """Return ConnectorConfig or None."""
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig
    import sqlalchemy as sa

    try:
        async with get_session_ctx() as session:
            result = await session.execute(
                sa.select(ConnectorConfig).where(
                    ConnectorConfig.company_id == company_id,
                    ConnectorConfig.connector == connector,
                )
            )
            row = result.first()
            return row[0] if row else None
    except Exception:
        return None


async def _ensure_connector_config(company_id: str, connector: str, category: str):
    """Get or create ConnectorConfig with sensible defaults."""
    from celerp.db import get_session_ctx
    from celerp.models.connector_config import ConnectorConfig
    import sqlalchemy as sa

    config = await _get_connector_config(company_id, connector)
    if config:
        return config

    default_freq = _DEFAULT_FREQUENCY.get(category, SyncFrequency.MANUAL).value
    config = ConnectorConfig(
        company_id=company_id,
        connector=connector,
        direction=SyncDirection.BOTH.value,
        sync_frequency=default_freq,
    )
    try:
        async with get_session_ctx() as session:
            session.add(config)
            await session.commit()
            await session.refresh(config)
    except Exception:
        config = await _get_connector_config(company_id, connector)
    return config


_HOW_IT_WORKS: dict[str, list[str]] = {
    "shopify": [
        "Enter your Shopify store domain below",
        "Authorize Celerp in the Shopify OAuth flow",
        "Products, orders, and customers sync automatically",
    ],
    "woocommerce": [
        "Enter your WooCommerce store URL",
        "Paste your REST API consumer key and secret",
        "Choose sync direction and frequency",
    ],
    "quickbooks": [
        "Click Connect to open QuickBooks authorization",
        "Sign in and grant access to your company file",
        "Invoices, contacts, and items sync on your schedule",
    ],
    "xero": [
        "Click Connect to open Xero authorization",
        "Select the Xero organization to link",
        "Invoices, contacts, and items sync on your schedule",
    ],
}

_ENTITY_LABELS: dict[str, str] = {
    "products": "Products",
    "orders": "Orders",
    "contacts": "Contacts",
    "inventory": "Inventory",
    "invoices": "Invoices",
}


def _connector_card(
    c: dict,
    last_run,
    relay_url: str,
    instance_id: str,
    config=None,
    lang: str = "en",
) -> FT:
    cid = c["id"]
    coming_soon = c.get("status") == "coming-soon"
    connected = c.get("connected", False)
    icon = _CONNECTOR_ICONS.get(cid, "🔌")
    category = c.get("category", "website")
    name = c.get("name", cid)
    description = c.get("description", "")
    entities = c.get("entities", [])

    direction = config.direction if config else SyncDirection.BOTH.value
    frequency = config.sync_frequency if config else _DEFAULT_FREQUENCY.get(category, SyncFrequency.MANUAL).value

    # ── Status badge ──────────────────────────────────────────────────────────
    if connected:
        status_badge = Span("● " + t("connectors.connected", lang), cls="connector-badge connector-badge--connected")
    elif coming_soon:
        status_badge = Span(t("connectors.coming_soon", lang), cls="connector-badge connector-badge--soon")
    else:
        status_badge = Span("○ " + t("connectors.not_connected", lang), cls="connector-badge connector-badge--idle")

    # ── Synced entities pills ─────────────────────────────────────────────────
    entity_pills = Div(
        *[Span(_ENTITY_LABELS.get(e, e), cls="connector-entity-pill") for e in entities],
        cls="connector-entity-row",
    )

    # ── How it works steps ────────────────────────────────────────────────────
    steps = _HOW_IT_WORKS.get(cid, [])
    steps_section = Ol(
        *[Li(s, cls="connector-step") for s in steps],
        cls="connector-steps",
    ) if steps and not connected else Span()

    # ── Connected details ─────────────────────────────────────────────────────
    connected_details = Span()
    if connected:
        sync_info = _last_sync_info(last_run)
        dir_row = _direction_toggle(cid, direction, lang)
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
            Button(
                t("connectors.sync_now", lang),
                cls="btn btn--sm btn--primary",
                hx_post=f"/settings/connectors/{cid}/sync",
                hx_target=f"#connector-sync-info-{cid}",
                hx_swap="innerHTML",
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
    elif c.get("auth_type") == "oauth":
        if cid == "shopify":
            # Shopify needs the shop domain first
            action_area = Div(
                Form(
                    Input(
                        name="shop",
                        placeholder=t("connectors.shop_url_placeholder", lang),
                        cls="input input--sm",
                        style="flex:1;min-width:220px;",
                    ),
                    Button(
                        t("connectors.connect", lang),
                        type="submit",
                        cls="btn btn--sm btn--primary",
                        style="margin-left:8px;white-space:nowrap;",
                    ),
                    hx_get=f"/settings/connectors/{cid}/oauth-redirect",
                    hx_target=f"#connector-card-{cid}",
                    hx_swap="outerHTML",
                    style="display:flex;align-items:center;gap:4px;",
                ),
                cls="connector-action-area",
            )
        else:
            action_area = Div(
                Button(
                    t("connectors.connect", lang),
                    cls="btn btn--sm btn--primary",
                    hx_get=f"/settings/connectors/{cid}/oauth-redirect",
                    hx_target=f"#connector-card-{cid}",
                    hx_swap="outerHTML",
                ),
                cls="connector-action-area",
            )
    else:
        # API key auth (WooCommerce)
        action_area = Div(
            Form(
                Input(name="store_url", placeholder="https://mystore.com",
                      cls="input input--sm", style="flex:1;min-width:200px;"),
                Input(name="consumer_key", placeholder="Consumer Key",
                      cls="input input--sm", style="width:160px;"),
                Input(name="consumer_secret", placeholder="Consumer Secret",
                      type="password", cls="input input--sm", style="width:160px;"),
                Button(t("connectors.connect", lang),
                       type="submit", cls="btn btn--sm btn--primary"),
                hx_post=f"/settings/connectors/{cid}/connect-apikey",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;flex-wrap:wrap;gap:6px;",
            ),
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


async def connectors_tab_content(lang: str = "en", token: str = "") -> FT:
    """Render the full connectors tab (catalog grouped by category)."""
    from celerp.config import ensure_instance_id
    from celerp.gateway.client import get_client
    from ui.config import RELAY_URL

    gw = get_client()
    relay_url = RELAY_URL
    iid = ensure_instance_id()

    catalog, _fetch_err = await _fetch_catalog(relay_url, iid, token=token)

    if not catalog:
        return Div(
            P(t("connectors.fetch_error", lang,
                default="Could not load connectors from relay. Check your connection."),
              cls="flash flash--warning"),
            cls="settings-card",
        )

    last_runs = await _get_last_runs(iid)

    # Load configs for all connected connectors
    configs: dict[str, object] = {}
    for c in catalog:
        if c.get("connected"):
            cfg = await _ensure_connector_config(iid, c["id"], c.get("category", "website"))
            configs[c["id"]] = cfg

    # Group by category, labelled by what the sync does for the customer
    group_labels = {
        "website": t("connectors.group_website", lang, default="Website Sync"),
        "accounting": t("connectors.group_accounting", lang, default="Accounting Sync"),
    }
    categories: dict[str, list[dict]] = {}
    for c in catalog:
        categories.setdefault(c.get("category", "other"), []).append(c)

    # Stable order: website first, then accounting, then anything else
    order = ["website", "accounting"]
    ordered_cats = sorted(
        categories, key=lambda k: (order.index(k) if k in order else len(order), k)
    )

    sections: list[FT] = []
    for cat in ordered_cats:
        label = group_labels.get(cat, cat.title())
        cards = [
            _connector_card(c, last_runs.get(c["id"]), relay_url, iid,
                          config=configs.get(c["id"]), lang=lang)
            for c in categories[cat]
        ]
        sections.append(
            Div(
                P(label, cls="connector-section-title"),
                *cards,
            )
        )

    return Div(
        *sections,
        cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/connectors/tab")
    async def connectors_tab_htmx(request: Request):
        """HTMX partial: render connectors tab content."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        from ui.i18n import get_lang
        lang = get_lang(request)
        return await connectors_tab_content(lang)

    @app.get("/settings/connectors/{platform}/oauth-redirect")
    async def connector_oauth_redirect(request: Request, platform: str, shop: str = ""):
        """HTMX: get OAuth authorize URL and return JS to open it in a new tab."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        lang = get_lang(request)

        from ui.api_client import get_connector_authorize_url
        try:
            result = await get_connector_authorize_url(token, platform, shop=shop)
        except Exception as exc:
            return Span(str(exc), cls="flash flash--warning")

        if "error" in result:
            return Span(result["error"], cls="flash flash--warning")

        url = result.get("authorize_url", "")
        if not url:
            return Span(t("connectors.authorize_error", lang), cls="flash flash--warning")

        # Return script that opens the URL; card stays as-is (user returns after OAuth)
        return Script(f"window.open({url!r}, '_blank', 'noopener');")

    @app.post("/settings/connectors/{platform}/direction")
    async def connector_set_direction(request: Request, platform: str):
        """HTMX: update sync direction for a connector."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.config import ensure_instance_id
        from celerp.db import get_session_ctx
        from celerp.models.connector_config import ConnectorConfig
        from ui.config import RELAY_URL
        from ui.i18n import get_lang
        import sqlalchemy as sa

        iid = ensure_instance_id()
        lang = get_lang(request)
        form = await request.form()
        direction = form.get("direction", "both")

        if direction not in ("inbound", "outbound", "both"):
            direction = "both"

        async with get_session_ctx() as session:
            await session.execute(
                sa.update(ConnectorConfig)
                .where(
                    ConnectorConfig.company_id == iid,
                    ConnectorConfig.connector == platform,
                )
                .values(direction=direction)
            )
            await session.commit()

        # Re-render the card
        catalog, _fetch_err = await _fetch_catalog(RELAY_URL, iid, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(iid)
        config = await _get_connector_config(iid, platform)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, iid,
                              config=config, lang=lang)

    @app.post("/settings/connectors/{platform}/frequency")
    async def connector_set_frequency(request: Request, platform: str):
        """HTMX: update sync frequency for an accounting connector."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.config import ensure_instance_id
        from celerp.db import get_session_ctx
        from celerp.models.connector_config import ConnectorConfig
        from ui.config import RELAY_URL
        from ui.i18n import get_lang
        import sqlalchemy as sa

        iid = ensure_instance_id()
        lang = get_lang(request)
        form = await request.form()
        frequency = form.get("sync_frequency", "manual")

        if frequency not in ("manual", "daily"):
            frequency = "manual"

        async with get_session_ctx() as session:
            await session.execute(
                sa.update(ConnectorConfig)
                .where(
                    ConnectorConfig.company_id == iid,
                    ConnectorConfig.connector == platform,
                )
                .values(sync_frequency=frequency)
            )
            await session.commit()

        catalog, _fetch_err = await _fetch_catalog(RELAY_URL, iid, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(iid)
        config = await _get_connector_config(iid, platform)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, iid,
                              config=config, lang=lang)

    @app.delete("/settings/connectors/{platform}/disconnect")
    async def connector_disconnect(request: Request, platform: str):
        """HTMX: disconnect a connector by deleting its tokens on relay."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.config import ensure_instance_id
        from ui.config import RELAY_URL
        from ui.i18n import get_lang
        import httpx

        iid = ensure_instance_id()
        lang = get_lang(request)

        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.delete(
                    f"{RELAY_URL}/tokens/{platform}",
                    params={"instance_id": iid},
                )
        except Exception as exc:
            return Div(
                Span(f"✗ {exc}", cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        catalog, _fetch_err = await _fetch_catalog(RELAY_URL, iid, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(iid)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, iid, lang=lang)

    @app.post("/settings/connectors/{platform}/sync")
    async def connector_sync_now(request: Request, platform: str):
        """HTMX: trigger an immediate sync for a connector. Returns updated sync info."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.config import ensure_instance_id
        from ui.i18n import get_lang

        iid = ensure_instance_id()
        lang = get_lang(request)

        try:
            from celerp.connectors.registry import get as get_connector
            from celerp.connectors.base import ConnectorContext, SyncDirection
            from celerp.connectors.sync_runner import run_sync
            from ui.config import RELAY_URL

            connector = get_connector(platform)

            # Get direction config
            config = await _get_connector_config(iid, platform)
            direction = SyncDirection(config.direction) if config else SyncDirection.BOTH

            # Fetch live access token from relay
            token_data = await _fetch_access_token(RELAY_URL, iid, platform)
            ctx = ConnectorContext(
                company_id=iid,
                access_token=token_data["access_token"],
                store_handle=token_data.get("store_handle"),
            )

            import asyncio

            async def _do_sync():
                for entity_enum in connector.supported_entities:
                    try:
                        await run_sync(connector, ctx, entity_enum.value, direction=direction)
                    except Exception:
                        pass

            asyncio.create_task(_do_sync())
            msg = t("connectors.sync_started", lang, default="Sync started...")
        except Exception as exc:
            msg = f"✗ {exc}"

        return Span(msg, cls="connector-sync-info")

    @app.post("/settings/connectors/{platform}/connect-apikey")
    async def connector_connect_apikey(request: Request, platform: str):
        """HTMX: save API key credentials for a connector (e.g. WooCommerce)."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        if (err := _validate_platform(platform)):
            return err

        from celerp.config import ensure_instance_id
        from ui.config import RELAY_URL
        from ui.i18n import get_lang
        import httpx
        import os

        iid = ensure_instance_id()
        lang = get_lang(request)
        form = await request.form()
        store_url = form.get("store_url", "").strip()
        consumer_key = form.get("consumer_key", "").strip()
        consumer_secret = form.get("consumer_secret", "").strip()

        if not consumer_key or not consumer_secret:
            return Div(
                Span(t("connectors.missing_credentials", lang, default="Consumer key and secret are required."),
                     cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        if not RELAY_URL.startswith("https://"):
            if not os.environ.get("CELERP_ALLOW_HTTP_RELAY"):
                return Div(
                    Span(t("settings.relay_url_must_use_https_set_celerpallowhttprelay1"),
                         cls="flash flash--warning"),
                    id=f"connector-card-{platform}",
                    cls="connector-card",
                )

        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(
                    f"{RELAY_URL}/tokens/{platform}",
                    json={
                        "instance_id": iid,
                        "consumer_key": consumer_key,
                        "consumer_secret": consumer_secret,
                        "store_url": store_url or None,
                    },
                )
        except Exception as exc:
            return Div(
                Span(f"✗ {exc}", cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        # Create connector config with defaults
        catalog, _fetch_err = await _fetch_catalog(RELAY_URL, iid, token=token)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        config = await _ensure_connector_config(iid, platform, c_data.get("category", "website"))
        last_runs = await _get_last_runs(iid)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, iid,
                              config=config, lang=lang)
