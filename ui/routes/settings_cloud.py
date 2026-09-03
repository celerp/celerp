# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Settings - Web Access: Celerp Connect connection, TOS, Team infrastructure."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from ui.components.shell import base_shell, page_header, page_title
from ui.i18n import t, get_lang

from ui.routes.settings import (
    _check_permission,
    _token,
    _cloud_relay_tab,
    PAID_TIERS,
)
from ui.routes.settings_general import _section_breadcrumb


def _has_team_features() -> bool:
    """Check if Team-tier infrastructure features are available (in-memory, no I/O)."""
    from celerp.gateway.state import get_feature_flags
    flags = get_feature_flags()
    return bool(flags.get("external_db") or flags.get("external_storage"))


def _cloud_tabs(active: str, has_team_features: bool = False, lang: str = "en") -> FT:
    tabs = [("status", t("settings.team_url", lang))]
    if has_team_features:
        tabs.append(("infrastructure", t("cloud.tab_infrastructure", lang)))
    # One tab per connector category, labelled by what the sync does for the
    # customer (same keys as the old in-tab section headings).
    tabs.append(("website", t("connectors.group_website", lang)))
    tabs.append(("accounting", t("connectors.group_accounting", lang)))
    # Payments lives with Web Access: it only works cloud-connected, and a
    # not-yet-connected visitor lands amid the plans/features pitch instead of
    # a dead Connect button. Links to its own page (needs live status).
    tabs.append(("payments", t("nav.payments", lang)))
    return Div(
        *[
            A(label,
              href="/settings/payments" if key == "payments" else f"/settings/cloud?tab={key}",
              cls=f"tab {'tab--active' if key == active else ''}")
            for key, label in tabs
        ],
        cls="settings-tabs",
    )


def _feature_card(icon: str, title: str, desc: str, lang: str = "en") -> FT:
    return Div(
        Div(icon, cls="cloud-feature-card__icon"),
        Div(title, cls="cloud-feature-card__title"),
        Div(desc, cls="cloud-feature-card__desc"),
        cls="cloud-feature-card",
    )


def _plan_card(name: str, price: str, desc: str, bullets: list[str], subscribe_url: str, featured: bool = False, lang: str = "en") -> FT:
    card_cls = "cloud-plan-card cloud-plan-card--featured" if featured else "cloud-plan-card"
    return Div(
        Div(name, cls="cloud-plan-card__name"),
        Div(price, Span(t("settings_cloud.per_mo", lang)), cls="cloud-plan-card__price"),
        Div(desc, cls="cloud-plan-card__desc"),
        Ul(*[Li(b) for b in bullets]),
        A(t("cloud.start_trial", lang), href=subscribe_url, target="_blank", cls="btn btn--primary btn--sm"),
        cls=card_cls,
    )


def _value_prop_page(iid: str, lang: str = "en", disconnected: bool = False) -> FT:
    """Full value-proposition landing page shown when not connected to cloud.

    `disconnected` marks a sticky Cloud disconnect: the credential is preserved,
    so the connect section withholds its auto-connect (landing here must not
    silently undo the disconnect) while the Connect button still reconnects in
    one click."""
    return Div(
        # Hero - explain the relay concept simply
        Div(
            H2(t("cloud.hero_title", lang)),
            P(t("cloud.hero_desc1", lang)),
            P(
                t("cloud.hero_desc2", lang),
                style="font-weight:600;margin-top:8px;",
            ),
            cls="cloud-hero",
        ),
        _plans_ad(iid, lang=lang),
        # Already subscribed / connect section
        _connect_section(iid, lang=lang, disconnected=disconnected),
        cls="content-area",
    )


def _plans_ad(iid: str, lang: str = "en") -> FT:
    """The paid-plan advertisement: feature cards, trial banner, plan cards.
    Shown on the not-connected landing page and, below the status tab, to
    connected free-tier accounts (the plans are what they are missing)."""
    from celerp.gateway.state import build_subscribe_url

    return Div(
        # Feature cards - three platform features on top...
        Div(
            _feature_card(
                "🔗", t("cloud.feature_url_title", lang),
                t("cloud.feature_url_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "💾", t("cloud.feature_backup_title", lang),
                t("cloud.feature_backup_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "🤖", t("cloud.feature_ai_title", lang),
                t("cloud.feature_ai_desc", lang),
                lang=lang,
            ),
            cls="cloud-features",
        ),
        # ...and the sync + payments features below
        Div(
            _feature_card(
                "🛒", t("cloud.feature_website_title", lang),
                t("cloud.feature_website_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "📊", t("cloud.feature_accounting_title", lang),
                t("cloud.feature_accounting_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "💳", t("cloud.feature_payments_title", lang),
                t("cloud.feature_payments_desc", lang),
                lang=lang,
            ),
            cls="cloud-features",
        ),
        # Plans - the centred trial banner introduces them; no left-aligned heading needed
        Div(
            Div(t("cloud.trial_head", lang), cls="cloud-trial-banner__head"),
            Div(t("cloud.trial_sub", lang), cls="cloud-trial-banner__sub"),
            cls="cloud-trial-banner",
        ),
        Div(
            _plan_card(
                t("cloud.plan_cloud_name", lang), "USD $29",
                t("cloud.plan_cloud_desc", lang),
                [
                    t("cloud.plan_cloud_b1", lang),
                    t("cloud.plan_cloud_b2", lang),
                    t("cloud.plan_cloud_b3", lang),
                    t("cloud.plan_cloud_b4", lang),
                ],
                build_subscribe_url(iid, extra="plan=cloud"),
                lang=lang,
            ),
            _plan_card(
                t("cloud.plan_ai_name", lang), "USD $49",
                t("cloud.plan_ai_desc", lang),
                [
                    t("cloud.plan_ai_b1", lang),
                    t("cloud.plan_ai_b2", lang),
                    t("cloud.plan_ai_b3", lang),
                ],
                build_subscribe_url(iid, extra="plan=ai"),
                featured=True,
                lang=lang,
            ),
            cls="cloud-plans",
        ),
    )


def _connect_section(iid: str, lang: str = "en", disconnected: bool = False) -> FT:
    """The Celerp-account surface in its claim-led variant (this page's context
    is an existing/prospective subscriber). ONE component app-wide - see
    ui/routes/account.py. Keeps id="cloud-relay-tab" so the shipped
    cloud-activate/cloud-claim responses replace the same element."""
    from ui.routes.account import account_panel
    return account_panel(lang, intent="claim", panel_id="cloud-relay-tab",
                         suppress_autoconnect=disconnected)


def _parse_db_url(url: str) -> dict:
    """Parse a postgresql+asyncpg://user:pass@host:port/dbname URL into components."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        return {
            "host": parsed.hostname or "",
            "port": str(parsed.port or 5432),
            "name": parsed.path.lstrip("/") if parsed.path else "",
            "user": parsed.username or "",
            # password intentionally omitted (masked in UI)
        }
    except Exception:
        return {"host": "", "port": "5432", "name": "", "user": ""}


def _infra_db_section() -> FT:
    from celerp.config import settings, read_config
    current_url = settings.database_url
    db = _parse_db_url(current_url)

    masked_url = current_url
    if "@" in current_url:
        try:
            from urllib.parse import urlparse
            p = urlparse(current_url)
            masked_url = current_url.replace(f":{p.password}@", ":****@") if p.password else current_url
        except Exception:
            pass

    # Check if there's a previous URL to restore
    cfg = read_config()
    prev_url = cfg.get("database_backup", {}).get("previous_url", "")

    return Div(
        H3(t("page.database")),
        P(t("settings.current"),
            Code(masked_url, style="font-size:12px;"),
            cls="settings-hint",
        ),
        P(t("settings._changing_the_database_requires_a_restart_and_data"),
            cls="flash flash--warning",
            style="margin-bottom:12px;",
        ),
        Form(
            Div(
                Label(t("label.host"), For="db_host"),
                Input(id="db_host", name="db_host", placeholder="localhost",
                      value=db["host"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.port"), For="db_port"),
                Input(id="db_port", name="db_port", type="number", value=db["port"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.database_name"), For="db_name"),
                Input(id="db_name", name="db_name", placeholder="celerp",
                      value=db["name"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.username"), For="db_user"),
                Input(id="db_user", name="db_user", placeholder="celerp",
                      value=db["user"], cls="input"),
                cls="form-row",
            ),
            Div(
                Label(t("label.password"), For="db_pass"),
                Input(id="db_pass", name="db_pass", type="password", placeholder="••••••••", cls="input"),
                cls="form-row",
            ),
            Div(
                Button(t("btn.test_connection"),
                    type="button",
                    cls="btn btn--outline btn--sm",
                    hx_post="/settings/cloud/test-db",
                    hx_include="closest form",
                    hx_target="#db-test-result",
                    hx_swap="innerHTML",
                ),
                Button(t("btn.save_restart"), type="submit", cls="btn btn--primary btn--sm", style="margin-left:8px;",
                       hx_confirm=t("settings_cloud.restart_server_confirm")),
                style="display:flex;align-items:center;margin-top:4px;",
            ),
            Div(id="db-test-result", cls="infra-test-result"),
            hx_post="/settings/cloud/save-infra",
            hx_target="#db-test-result",
            cls="infra-form",
        ),
        # Restore previous button (GDR undo support)
        Div(
            Button(t("btn._restore_previous_db_settings"),
                cls="btn btn--outline btn--sm",
                hx_post="/settings/cloud/restore-db",
                hx_target="#db-test-result",
                hx_swap="innerHTML",
                hx_confirm=t("settings_cloud.restore_db_confirm"),
            ),
            Div(id="db-test-result", cls="infra-test-result"),
            style="margin-top:8px;",
        ) if prev_url else "",
        cls="infra-section",
    )


def _infra_storage_section() -> FT:
    from celerp.config import settings
    backend = settings.storage_backend or "local"

    return Div(
        H3(t("page.file_storage")),
        Form(
            Div(
                Label(t("label.backend"), For="storage_backend"),
                Select(
                    Option(t("settings.local_filesystem"), value="local", selected=backend == "local"),
                    Option("S3-Compatible", value="s3", selected=backend == "s3"),
                    id="storage_backend",
                    name="storage_backend",
                    cls="input",
                    onchange="document.getElementById('s3-fields').style.display=this.value==='s3'?'block':'none';",
                ),
                cls="form-row",
            ),
            Div(
                Div(
                    Label(t("label.endpoint_url"), For="s3_endpoint"),
                    Input(id="s3_endpoint", name="s3_endpoint",
                          placeholder="https://s3.amazonaws.com", value=settings.storage_s3_endpoint,
                          cls="input"),
                    cls="form-row",
                ),
                Div(
                    Label(t("label.bucket_name"), For="s3_bucket"),
                    Input(id="s3_bucket", name="s3_bucket", placeholder="my-celerp-bucket",
                          value=settings.storage_s3_bucket, cls="input"),
                    cls="form-row",
                ),
                Div(
                    Label(t("label.access_key"), For="s3_access_key"),
                    Input(id="s3_access_key", name="s3_access_key", placeholder="AKIAIOSFODNN7EXAMPLE",
                          value=settings.storage_s3_access_key, cls="input"),
                    cls="form-row",
                ),
                Div(
                    Label(t("label.secret_key"), For="s3_secret_key"),
                    Input(id="s3_secret_key", name="s3_secret_key", type="password",
                          placeholder="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", cls="input"),
                    cls="form-row",
                ),
                id="s3-fields",
                style=f"display:{'block' if backend == 's3' else 'none'};",
            ),
            Div(
                Button(t("btn.test_connection"),
                    type="button",
                    cls="btn btn--outline btn--sm",
                    hx_post="/settings/cloud/test-storage",
                    hx_include="closest form",
                    hx_target="#storage-test-result",
                    hx_swap="innerHTML",
                ),
                Button(t("btn.save"), type="submit", cls="btn btn--primary btn--sm", style="margin-left:8px;"),
                style="display:flex;align-items:center;margin-top:4px;",
            ),
            Div(id="storage-test-result", cls="infra-test-result"),
            hx_post="/settings/cloud/save-infra",
            hx_target="#storage-test-result",
            cls="infra-form",
        ),
        cls="infra-section",
    )


def _infrastructure_tab() -> FT:
    """Team plan infrastructure config: external DB + S3 storage."""
    return Div(
        _infra_db_section(),
        _infra_storage_section(),
        cls="settings-card",
    )


def _backup_summary_card(gw_ok: bool = False, backup_data: dict | None = None) -> FT:
    """Compact backup status card for the cloud settings page."""

    if not gw_ok or backup_data is None:
        return Div(cls="settings-card")  # nothing to show when not connected

    def _last_run(entry: dict) -> str:
        last = entry.get("last_run")
        ok = entry.get("ok")
        if last is None:
            return t("settings.pending")
        from datetime import datetime, timezone
        try:
            dt = datetime.fromisoformat(last).astimezone(timezone.utc)
            stamp = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            stamp = last
        if ok is False:
            err = entry.get("error") or ""
            return f"{stamp} - {t('settings.failed')}{': ' + err if err else ''}"
        return stamp

    def _time_until(iso: str | None) -> str:
        if iso is None:
            return t("settings.not_scheduled")
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso)
        delta = dt - datetime.now(timezone.utc)
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        return t("settings_cloud.in_hours_mins", hours=hours, mins=mins) if hours > 0 else t("settings_cloud.in_mins", mins=mins)

    cloud_section = Div(
        Div(
            H4(t("page.backup"), style="margin:0;"),
            A(t("settings.view_full_backup_settings"), href="/settings/general?tab=backup",
              cls="settings-hint", style="font-size:0.82rem;"),
            style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;",
        ),
        Table(
            Tr(Td(t("settings.last_db_backup"), cls="detail-label"), Td(_last_run(backup_data["db"]))),
            Tr(Td(t("settings.next_db_backup"), cls="detail-label"), Td(_time_until(backup_data["next_db_utc"]))),
            cls="detail-table",
        ),
    )

    return Div(
        cloud_section,
        cls="settings-card",
    )


async def _relay_state(token) -> tuple[str, str, str, bool, bool]:
    """Fetch relay state from the API process (the gateway client lives there).

    Returns (relay_status, public_url, tier, disconnected, token_bound); on an
    unreachable API, degrades to the local client's status alone.
    """
    from celerp.gateway.client import get_client as _local_get_client
    import ui.api_client as _api
    from ui.api_client import APIError as _APIError
    relay_status = "inactive"
    public_url = ""
    tier = ""
    disconnected = False
    token_bound = False
    try:
        rs = await _api.get_relay_status(token)
        relay_status = rs.get("relay_status", "inactive")
        public_url = rs.get("public_url", "")
        tier = rs.get("tier") or ""
        disconnected = bool(rs.get("cloud_disconnected"))
        token_bound = bool(rs.get("gateway_token_set"))
    except (_APIError, Exception):
        lc = _local_get_client()
        relay_status = lc.relay_status if lc else "inactive"
    return relay_status, public_url, tier, disconnected, token_bound


def setup_routes(app):

    @app.get("/settings/cloud-relay-tab")
    async def cloud_relay_tab_fragment(request: Request):
        """HTMX fragment: re-render the relay tab with fresh state.

        Polled by the connecting card so the connection outcome (account view
        or the failure card) appears without a manual page reload.
        """
        token = _token(request)
        if not token:
            return Response(status_code=401)
        if await _check_permission(request, "manage_integrations"):
            return Div(id="cloud-relay-tab")
        relay_status, public_url, tier, _, token_bound = await _relay_state(token)
        return _cloud_relay_tab(relay_status=relay_status, public_url=public_url,
                                tier=tier, token_bound=token_bound)

    @app.get("/settings/cloud")
    async def settings_cloud_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := await _check_permission(request, "manage_integrations")):
            return r

        import ui.api_client as _api
        lang = get_lang(request)
        relay_status, public_url, tier, disconnected, token_bound = await _relay_state(token)
        # A free tier is signed in (holds a gateway_token) but never starts the WS
        # client - it has no tunnel to serve - so relay_status stays "inactive".
        # Treat a token-bound instance as connected so a signed-in free account
        # gets the account/disconnect view plus the upgrade ad, not the landing page.
        gw_ok = relay_status in ("active", "tos_required", "connecting", "error") or token_bound

        # If not connected, show value-prop landing. A sticky-disconnected install
        # keeps its preserved credential, so the connect section withholds its
        # auto-connect (a page visit must not silently undo the disconnect) while
        # the Connect button still reconnects in one click.
        if not gw_ok:
            from celerp.config import ensure_instance_id
            iid = ensure_instance_id()
            return await base_shell(
                _section_breadcrumb(t("settings_cloud.web_access", lang)),
                page_header(t("settings_cloud.web_access", lang)),
                _value_prop_page(iid, lang=lang, disconnected=disconnected),
                title=page_title("settings_cloud.web_access"),
                nav_active="web-access",
                lang=lang,
                request=request,
            )

        # Connected or connecting - show tabs
        tab = request.query_params.get("tab", "status")
        has_team = _has_team_features()

        if tab == "infrastructure" and has_team:
            content = _infrastructure_tab()
        elif tab in ("website", "accounting"):
            from ui.routes.settings_connectors import connectors_tab_content
            content = await connectors_tab_content(lang, token=token, category=tab)
        else:
            backup_data: dict | None = None
            try:
                backup_data = await _api.get_backup_status(token)
            except Exception:
                pass
            # Backups are a paid-tier feature (like connectors); a free instance has
            # no public_url and no backup entitlement, so the summary card is omitted
            # entirely rather than showing scheduler/pending state for a plan that
            # never runs backups.
            parts = [_cloud_relay_tab(relay_status=relay_status, public_url=public_url, tier=tier, token_bound=token_bound),
                     _backup_summary_card(gw_ok=gw_ok and bool(public_url), backup_data=backup_data)]
            # A connected free-tier account keeps its free tabs but still sees
            # the paid-plan advertisement the not-connected page carries - the
            # plans are exactly what the free tier is missing. An unknown tier
            # (status round trip failed/pending) degrades to showing the ad,
            # never to silently hiding it - only a confirmed paid tier suppresses it.
            if tier not in PAID_TIERS:
                from celerp.config import ensure_instance_id
                parts.append(_plans_ad(ensure_instance_id(), lang=lang))
            content = Div(*parts)
            tab = "status"

        return await base_shell(
            _section_breadcrumb(t("settings_cloud.web_access", lang)),
            page_header(t("settings_cloud.web_access", lang)),
            _cloud_tabs(tab, has_team_features=has_team, lang=lang),
            content,
            title=page_title("settings_cloud.web_access"),
            nav_active="web-access",
            lang=lang,
            request=request,
        )

    @app.post("/settings/cloud/test-db")
    async def cloud_test_db(request: Request):
        """HTMX: test database connectivity with provided credentials."""
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        form = await request.form()
        host = form.get("db_host", "").strip()
        port = int(form.get("db_port", "5432") or "5432")
        name = form.get("db_name", "").strip()
        user = form.get("db_user", "").strip()
        password = form.get("db_pass", "")

        if not all([host, name, user]):
            return Span(t("settings.please_fill_in_host_database_name_and_username"),
                        cls="infra-test-result--err")

        import asyncio
        try:
            conn = await asyncio.wait_for(
                _try_db_connect(host, port, name, user, password),
                timeout=3.0,
            )
            return Span(t("settings_cloud.connected_to", target=f"{name}@{host}:{port}"), cls="infra-test-result--ok")
        except asyncio.TimeoutError:
            return Span(t("settings.connection_timed_out_3s"), cls="infra-test-result--err")
        except Exception as exc:
            return Span(f"✗ {type(exc).__name__}: {exc}", cls="infra-test-result--err")

    @app.post("/settings/cloud/test-storage")
    async def cloud_test_storage(request: Request):
        """HTMX: test S3-compatible storage connectivity."""
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        form = await request.form()
        backend = form.get("storage_backend", "local")
        if backend == "local":
            return Span(t("settings._local_filesystem_no_connection_needed"), cls="infra-test-result--ok")

        endpoint = form.get("s3_endpoint", "").strip()
        bucket = form.get("s3_bucket", "").strip()
        access_key = form.get("s3_access_key", "").strip()
        secret_key = form.get("s3_secret_key", "")

        if not all([endpoint, bucket, access_key, secret_key]):
            return Span(t("settings.please_fill_in_all_s3_fields"), cls="infra-test-result--err")

        import asyncio
        try:
            msg = await asyncio.wait_for(
                _try_s3_connect(endpoint, bucket, access_key, secret_key),
                timeout=3.0,
            )
            return Span(msg, cls="infra-test-result--ok")
        except asyncio.TimeoutError:
            return Span(t("settings.connection_timed_out_3s"), cls="infra-test-result--err")
        except Exception as exc:
            return Span(f"✗ {exc}", cls="infra-test-result--err")

    @app.post("/settings/cloud/save-infra")
    async def cloud_save_infra(request: Request):
        """Save infrastructure config (DB + storage) to config.toml."""
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        form = await request.form()
        try:
            from celerp.config import read_config, write_config, settings
            cfg = read_config()
            if not cfg:
                return Span(t("settings.no_config_file_found"), cls="infra-test-result--err")

            db_url_changed = False

            # DB settings: compose URL when host+name+user are all present
            host = form.get("db_host", "").strip()
            name = form.get("db_name", "").strip()
            user = form.get("db_user", "").strip()
            if host and name and user:
                port = form.get("db_port", "5432").strip() or "5432"
                password = form.get("db_pass", "")
                new_url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"
                previous_url = cfg.get("database", {}).get("url", settings.database_url)
                if new_url != previous_url:
                    # Backup previous URL for undo support
                    cfg.setdefault("database_backup", {})["previous_url"] = previous_url
                    cfg.setdefault("database", {})["url"] = new_url
                    db_url_changed = True

            # Storage settings
            storage_backend = form.get("storage_backend", "")
            if storage_backend:
                prev_storage = cfg.get("storage", {})
                cfg.setdefault("storage_backup", {}).update({
                    "backend": prev_storage.get("backend", ""),
                    "s3_endpoint": prev_storage.get("s3_endpoint", ""),
                    "s3_bucket": prev_storage.get("s3_bucket", ""),
                    "s3_access_key": prev_storage.get("s3_access_key", ""),
                    "s3_secret_key": prev_storage.get("s3_secret_key", ""),
                })
                cfg.setdefault("storage", {})["backend"] = storage_backend
                cfg["storage"]["s3_endpoint"] = form.get("s3_endpoint", "")
                cfg["storage"]["s3_bucket"] = form.get("s3_bucket", "")
                cfg["storage"]["s3_access_key"] = form.get("s3_access_key", "")
                if form.get("s3_secret_key"):
                    cfg["storage"]["s3_secret_key"] = form.get("s3_secret_key")

            write_config(cfg)

            if db_url_changed:
                import subprocess
                subprocess.Popen(["pkill", "-HUP", "-f", "uvicorn"])

            return Span(t("settings._saved"), cls="infra-test-result--ok")
        except Exception as exc:
            return Span(t("settings_cloud.save_failed", err=exc), cls="infra-test-result--err")

    @app.post("/settings/cloud/restore-db")
    async def cloud_restore_db(request: Request):
        """Restore the previous database URL (GDR undo support)."""
        token = _token(request)
        # Infra changes (DB/storage endpoints) are admin/owner actions - the
        # page is role-gated, so its fragments must be too.
        if await _check_permission(request, "manage_integrations"):
            return Div()
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        try:
            from celerp.config import read_config, write_config
            cfg = read_config()
            if not cfg:
                return Span(t("settings.no_config_file_found"), cls="infra-test-result--err")

            prev_url = cfg.get("database_backup", {}).get("previous_url", "")
            if not prev_url:
                return Span(t("settings.no_previous_database_url_to_restore"), cls="infra-test-result--err")

            current_url = cfg.get("database", {}).get("url", "")
            cfg.setdefault("database_backup", {})["previous_url"] = current_url
            cfg.setdefault("database", {})["url"] = prev_url
            write_config(cfg)

            import subprocess
            subprocess.Popen(["pkill", "-HUP", "-f", "uvicorn"])

            return Span(t("settings._restored_previous_db_url_restarting"), cls="infra-test-result--ok")
        except Exception as exc:
            return Span(t("settings_cloud.restore_failed", err=exc), cls="infra-test-result--err")


async def _try_db_connect(host: str, port: int, name: str, user: str, password: str) -> None:
    """Attempt an asyncpg connection to verify credentials."""
    import asyncpg  # type: ignore[import]
    conn = await asyncpg.connect(
        host=host, port=port, database=name, user=user, password=password
    )
    await conn.close()


async def _try_s3_connect(endpoint: str, bucket: str, access_key: str, secret_key: str) -> str:
    """Test S3-compatible storage connectivity with meaningful error messages."""
    import httpx

    url = endpoint.rstrip("/")
    bucket_url = f"{url}/{bucket}"

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            r = await client.head(bucket_url, headers={"Authorization": "dummy"})
    except httpx.ConnectError:
        raise RuntimeError(t("settings_cloud.cannot_reach_endpoint"))
    except httpx.TimeoutException:
        raise RuntimeError(t("settings_cloud.cannot_reach_endpoint"))

    if r.status_code == 200:
        return t("settings_cloud.connected_to_bucket", bucket=bucket)
    elif r.status_code == 403:
        raise RuntimeError(t("settings_cloud.invalid_credentials_403"))
    elif r.status_code == 404:
        raise RuntimeError(t("settings_cloud.bucket_not_found_404"))
    elif r.status_code in (301, 307, 308):
        # Redirect - endpoint reachable but bucket may be in different region
        raise RuntimeError(t("settings_cloud.bucket_redirect", status=r.status_code))
    else:
        raise RuntimeError(t("settings_cloud.s3_returned", status=r.status_code))
