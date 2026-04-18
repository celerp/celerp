# SPDX-License-Identifier: BSL-1.1
"""Settings - Web Access: Connectors tab."""

from __future__ import annotations

from datetime import datetime, timezone

from fasthtml.common import *
from starlette.requests import Request

from ui.i18n import t
from ui.routes.settings import _check_role, _token

_CONNECTOR_ICONS: dict[str, str] = {
    "shopify": "🛍️",
    "woocommerce": "🛒",
    "quickbooks": "📊",
    "xero": "📗",
}


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
        delta = datetime.now(timezone.utc) - finished.replace(tzinfo=timezone.utc) if finished.tzinfo is None else datetime.now(timezone.utc) - finished
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


async def _fetch_catalog(relay_url: str, instance_id: str) -> list[dict]:
    """Fetch connector catalog from relay. Returns empty list on failure."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            r = await c.get(
                f"{relay_url}/api/connectors",
                params={"instance_id": instance_id},
            )
            if r.status_code == 200:
                return r.json().get("connectors", [])
    except Exception:
        pass
    return []


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


def _connector_card(
    c: dict,
    last_run,
    relay_url: str,
    instance_id: str,
    lang: str = "en",
) -> FT:
    cid = c["id"]
    coming_soon = c.get("status") == "coming-soon"
    connected = c.get("connected", False)
    icon = _CONNECTOR_ICONS.get(cid, "🔌")

    # Entity chips
    entities_row = Div(
        *[_entity_chip(e) for e in c.get("entities", [])],
        cls="connector-entities",
    )

    # Status area
    status_row = Div(
        _status_badge(connected, lang),
        _coming_soon_badge(lang) if coming_soon else "",
        cls="connector-status-row",
    )

    # Last sync
    sync_info = _last_sync_info(last_run)

    # Action buttons
    if coming_soon:
        action_btns = Div(
            Button(t("connectors.connect", lang, default="Connect"),
                   cls="btn btn--sm btn--outline", disabled=True),
            cls="connector-actions",
        )
    elif connected:
        action_btns = Div(
            Button(
                t("connectors.disconnect", lang, default="Disconnect"),
                cls="btn btn--sm btn--outline btn--danger",
                hx_delete=f"/settings/connectors/{cid}/disconnect",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                hx_confirm=t("connectors.disconnect_confirm", lang, default="Disconnect and remove stored tokens?"),
            ),
            Button(
                t("connectors.sync_now", lang, default="Sync Now"),
                cls="btn btn--sm btn--primary",
                hx_post=f"/settings/connectors/{cid}/sync",
                hx_target=f"#connector-sync-info-{cid}",
                hx_swap="innerHTML",
                hx_indicator=f"#connector-sync-spinner-{cid}",
            ),
            Span("⏳", id=f"connector-sync-spinner-{cid}",
                 cls="htmx-indicator", style="margin-left:6px;display:none;"),
            cls="connector-actions",
        )
    elif c.get("auth_type") == "oauth":
        oauth_url = f"{relay_url}/oauth/{cid}/initiate?instance_id={instance_id}"
        action_btns = Div(
            A(
                t("connectors.connect", lang, default="Connect"),
                href=oauth_url,
                target="_blank",
                cls="btn btn--sm btn--primary",
            ),
            cls="connector-actions",
        )
    else:
        # api_key type - show form
        action_btns = Div(
            Form(
                Input(name="consumer_key", placeholder="Consumer Key",
                      cls="input input--sm", style="width:180px;"),
                Input(name="consumer_secret", placeholder="Consumer Secret",
                      type="password", cls="input input--sm",
                      style="width:180px;margin-left:6px;"),
                Button(t("connectors.connect", lang, default="Connect"),
                       type="submit", cls="btn btn--sm btn--primary",
                       style="margin-left:6px;"),
                hx_post=f"/settings/connectors/{cid}/connect-apikey",
                hx_target=f"#connector-card-{cid}",
                hx_swap="outerHTML",
                style="display:flex;align-items:center;flex-wrap:wrap;gap:4px;",
            ),
            cls="connector-actions",
        )

    card_cls = "connector-card"
    if coming_soon:
        card_cls += " connector-card--coming-soon"

    return Div(
        Div(
            Span(icon, cls="connector-icon"),
            Div(
                Strong(c.get("name", cid)),
                P(c.get("description", ""), cls="connector-desc settings-hint"),
                cls="connector-info",
            ),
            cls="connector-header",
        ),
        entities_row,
        status_row,
        Div(sync_info, id=f"connector-sync-info-{cid}"),
        action_btns,
        id=f"connector-card-{cid}",
        cls=card_cls,
    )


async def connectors_tab_content(lang: str = "en") -> FT:
    """Render the full connectors tab (catalog grouped by category)."""
    from celerp.config import ensure_instance_id
    from celerp.gateway.client import get_client
    from ui.config import RELAY_URL

    gw = get_client()
    relay_url = RELAY_URL
    iid = ensure_instance_id()

    catalog = await _fetch_catalog(relay_url, iid)

    if not catalog:
        return Div(
            P(t("connectors.fetch_error", lang,
                default="Could not load connectors from relay. Check your connection."),
              cls="flash flash--warning"),
            cls="settings-card",
        )

    # Get last sync runs - need a company_id; use instance_id as fallback
    last_runs = await _get_last_runs(iid)

    # Group by category
    from itertools import groupby
    categories: dict[str, list[dict]] = {}
    for c in catalog:
        cat = c.get("category", "other").title()
        categories.setdefault(cat, []).append(c)

    sections: list[FT] = []
    for cat_name, connectors in categories.items():
        cards = [
            _connector_card(c, last_runs.get(c["id"]), relay_url, iid, lang)
            for c in connectors
        ]
        sections.append(
            Div(
                H3(cat_name, cls="settings-section-title"),
                Div(*cards, cls="connector-cards-grid"),
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
            from fasthtml.common import P
            return P(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r
        from ui.i18n import get_lang
        lang = get_lang(request)
        return await connectors_tab_content(lang)

    @app.delete("/settings/connectors/{platform}/disconnect")
    async def connector_disconnect(request: Request, platform: str):
        """HTMX: disconnect a connector by deleting its tokens on relay."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r

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

        # Re-render card as disconnected
        catalog = await _fetch_catalog(RELAY_URL, iid)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(iid)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, iid, lang)

    @app.post("/settings/connectors/{platform}/sync")
    async def connector_sync_now(request: Request, platform: str):
        """HTMX: trigger an immediate sync for a connector. Returns updated sync info."""
        token = _token(request)
        if not token:
            return Span(t("error.unauthorized"), cls="flash flash--warning")
        if (r := _check_role(request, "admin")):
            return r

        from celerp.config import ensure_instance_id
        from ui.i18n import get_lang

        iid = ensure_instance_id()
        lang = get_lang(request)

        try:
            from celerp.connectors.registry import get_connector
            from celerp.connectors.base import ConnectorContext
            from celerp.connectors.sync_runner import run_sync

            connector = get_connector(platform)
            ctx = ConnectorContext(instance_id=iid, company_id=iid)

            import asyncio
            # Sync all entities in background (fire-and-forget for UI responsiveness)
            async def _do_sync():
                for entity in connector.entities:
                    try:
                        await run_sync(connector, ctx, entity)
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

        from celerp.config import ensure_instance_id
        from ui.config import RELAY_URL
        from ui.i18n import get_lang
        import httpx

        iid = ensure_instance_id()
        lang = get_lang(request)
        form = await request.form()

        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.post(
                    f"{RELAY_URL}/tokens/{platform}",
                    json={
                        "instance_id": iid,
                        "consumer_key": form.get("consumer_key", ""),
                        "consumer_secret": form.get("consumer_secret", ""),
                    },
                )
        except Exception as exc:
            return Div(
                Span(f"✗ {exc}", cls="flash flash--warning"),
                id=f"connector-card-{platform}",
                cls="connector-card",
            )

        catalog = await _fetch_catalog(RELAY_URL, iid)
        c_data = next((c for c in catalog if c["id"] == platform), {"id": platform, "name": platform})
        last_runs = await _get_last_runs(iid)
        return _connector_card(c_data, last_runs.get(platform), RELAY_URL, iid, lang)
