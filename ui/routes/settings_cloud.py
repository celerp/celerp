# SPDX-License-Identifier: BSL-1.1

"""Settings - Web Access: Cloud Relay connection, TOS, Team infrastructure."""

from __future__ import annotations

from fasthtml.common import *
from starlette.requests import Request
from starlette.responses import RedirectResponse

from ui.components.shell import base_shell, page_header
from ui.i18n import t, get_lang

from ui.routes.settings import (
    _check_role,
    _token,
    _cloud_relay_tab,
)
from ui.routes.settings_general import _section_breadcrumb


def _has_team_features() -> bool:
    """Check if Team-tier infrastructure features are available (in-memory, no I/O)."""
    from celerp.gateway.state import get_feature_flags
    flags = get_feature_flags()
    return bool(flags.get("external_db") or flags.get("external_storage"))


def _cloud_tabs(active: str, has_team_features: bool = False, lang: str = "en") -> FT:
    tabs = [("status", t("cloud.tab_connection", lang))]
    if has_team_features:
        tabs.append(("infrastructure", t("cloud.tab_infrastructure", lang)))
    tabs.append(("connectors", t("cloud.tab_connectors", lang, default="Connectors")))
    return Div(
        *[
            A(label, href=f"/settings/cloud?tab={key}",
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
        Div(price, Span("/mo"), cls="cloud-plan-card__price"),
        Div(desc, cls="cloud-plan-card__desc"),
        Ul(*[Li(b) for b in bullets]),
        A(t("settings.subscribe", lang), href=subscribe_url, target="_blank", cls="btn btn--primary btn--sm"),
        cls=card_cls,
    )


def _value_prop_page(iid: str, lang: str = "en") -> FT:
    """Full value-proposition landing page shown when not connected to cloud."""
    subscribe_base = f"https://celerp.com/subscribe?instance_id={iid}"

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
        # Feature cards
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
                "🔄", t("cloud.feature_connector_title", lang),
                t("cloud.feature_connector_desc", lang),
                lang=lang,
            ),
            _feature_card(
                "🤖", t("cloud.feature_ai_title", lang),
                t("cloud.feature_ai_desc", lang),
                lang=lang,
            ),
            cls="cloud-features",
        ),
        # Plans
        H3(t("page.plans", lang), style="margin-bottom:0;"),
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
                subscribe_base + "#cloud",
                lang=lang,
            ),
            _plan_card(
                t("cloud.plan_ai_name", lang), "USD $49",
                t("cloud.plan_ai_desc", lang),
                [
                    t("cloud.plan_ai_b1", lang),
                    t("cloud.plan_ai_b2", lang),
                    t("cloud.plan_ai_b3", lang),
                    t("cloud.plan_ai_b4", lang),
                ],
                subscribe_base + "#cloud-ai",
                featured=True,
                lang=lang,
            ),
            _plan_card(
                t("cloud.plan_team_name", lang), "USD $99",
                t("cloud.plan_team_desc", lang),
                [
                    t("cloud.plan_team_b1", lang),
                    t("cloud.plan_team_b2", lang),
                    t("cloud.plan_team_b3", lang),
                    t("cloud.plan_team_b4", lang),
                ],
                subscribe_base + "#team",
                lang=lang,
            ),
            cls="cloud-plans",
        ),
        # Already subscribed / connect section
        _connect_section(iid, lang=lang),
        cls="content-area",
    )


def _connect_section(iid: str, lang: str = "en") -> FT:
    """'Already subscribed?' block with both auto-connect button and email claim form.

    The outer div carries id="cloud-relay-tab" so HTMX responses from
    cloud-activate and cloud-claim can replace the entire block on success.
    """
    return Div(
        H4(t("page.already_subscribed", lang), style="margin:0 0 4px;"),
        P(t("settings.if_you_already_subscribed_on_the_website_we_can_li", lang),
            cls="settings-hint",
            style="margin-bottom:12px;",
        ),
        # Auto-connect button (tries to match by instance_id)
        Div(
            Button(t("btn.connect_automatically", lang),
                cls="btn btn--primary",
                hx_post="/settings/cloud-activate",
                hx_target="#cloud-relay-tab",
                hx_swap="outerHTML",
                hx_indicator="#cloud-connecting",
                id="cloud-connect-btn",
            ),
            Span(t("settings.connecting", lang), id="cloud-connecting",
                 cls="settings-hint htmx-indicator", style="margin-left:12px;display:none;"),
            style="margin-bottom:16px;",
        ),
        # Auto-trigger on first page load
        Script("""
(function(){
  if (sessionStorage.getItem('cloud_activate_tried')) return;
  sessionStorage.setItem('cloud_activate_tried', '1');
  var btn = document.getElementById('cloud-connect-btn');
  if (btn) htmx.trigger(btn, 'click');
})();
"""),
        # Email claim form (always visible)
        P(t("settings.or_enter_the_email_address_you_used_at_checkout", lang),
            cls="settings-hint",
        ),
        Form(
            Input(
                type="email",
                name="claim_email",
                placeholder="Email used at checkout",
                required=True,
                cls="input input--sm",
                style="width:260px;",
            ),
            Button(t("btn.link_subscription", lang), type="submit",
                   cls="btn btn--sm btn--outline", style="margin-left:8px;"),
            hx_post="/settings/cloud-send-otp",
            hx_target="#cloud-relay-tab",
            hx_swap="outerHTML",
            style="display:flex;align-items:center;margin-top:8px;",
        ),
        id="cloud-relay-tab",
        cls="cloud-connect-section",
    )


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
                       onclick="return confirm('This will restart the server. Continue?');"),
                style="display:flex;align-items:center;margin-top:4px;",
            ),
            Div(id="db-test-result", cls="infra-test-result"),
            hx_post="/settings/cloud/save-infra",
            hx_target="#db-test-result",
            cls="infra-form",
        ),
        # Restore previous button (GDR undo support)
        Div(
            Button("↩ Restore previous DB settings",
                cls="btn btn--outline btn--sm",
                hx_post="/settings/cloud/restore-db",
                hx_target="#db-test-result",
                hx_swap="innerHTML",
                hx_confirm="Restore the previous database URL and restart?",
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


def _backup_summary_card() -> FT:
    """Compact backup status card for the cloud settings page.

    Always shows local export/import. When cloud-connected and backup module
    loaded, also shows last backup results and a link to the full backup tab.
    """
    from celerp.gateway.client import get_client
    from ui.components.backup import local_backup_buttons
    gw_ok = get_client() is not None

    # Local backup section (always visible)
    local_section = Div(
        H4(t("page.local_backup"), style="margin:0 0 6px;"),
        P(t("settings.export_or_import_a_full_backup_of_your_database_an"), cls="settings-hint", style="margin-bottom:10px;"),
        local_backup_buttons(
            import_input_id="cloud-page-import-input",
            flash_target_id="cloud-page-backup-flash",
            btn_size="sm",
        ),
        Div(id="cloud-page-backup-flash", cls="mt-sm"),
    )

    if not gw_ok:
        return Div(local_section, cls="settings-card")

    # Cloud backup status (only when connected and backup module available)
    try:
        from celerp.services import backup_scheduler
        db_status = backup_scheduler.last_db_result()
        fl_status = backup_scheduler.last_file_result()
        next_db = backup_scheduler.next_db_run_utc()
        next_fl = backup_scheduler.next_file_run_utc()
    except Exception:
        return Div(local_section, cls="settings-card")

    def _status_badge(result) -> FT:
        if result.ok is None:
            return Span(t("settings.pending"), cls="badge badge--inactive")
        return Span("OK", cls="badge badge--active") if result.ok else Span(t("settings.failed"), cls="badge badge--error")

    def _time_until(dt) -> str:
        if dt is None:
            return "not scheduled"
        from datetime import datetime, timezone
        delta = dt - datetime.now(timezone.utc)
        hours = int(delta.total_seconds() // 3600)
        mins = int((delta.total_seconds() % 3600) // 60)
        return f"in {hours}h {mins}m" if hours > 0 else f"in {mins}m"

    cloud_section = Div(
        Div(
            H4(t("settings.tab_backup"), style="margin:0;"),
            A(t("settings.view_full_backup_settings"), href="/settings?tab=backup",
              cls="settings-hint", style="font-size:0.82rem;"),
            style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;",
        ),
        Table(
            Tr(Td(t("settings.last_db_backup"), cls="detail-label"), Td(_status_badge(db_status))),
            Tr(Td(t("settings.last_file_backup"), cls="detail-label"), Td(_status_badge(fl_status))),
            Tr(Td(t("settings.next_db_backup"), cls="detail-label"), Td(_time_until(next_db))),
            Tr(Td(t("settings.next_file_backup"), cls="detail-label"), Td(_time_until(next_fl))),
            cls="detail-table",
        ),
        style="margin-bottom:1rem;",
    )

    return Div(
        H3(t("page.backup"), cls="settings-section-title"),
        cloud_section,
        Hr(style="margin:1rem 0;border-color:var(--c-border);"),
        local_section,
        cls="settings-card",
    )


def setup_routes(app):

    @app.get("/settings/cloud")
    async def settings_cloud_page(request: Request):
        token = _token(request)
        if not token:
            return RedirectResponse("/login", status_code=302)
        if (r := _check_role(request, "admin")):
            return r

        from celerp.gateway.client import get_client
        gw = get_client()
        lang = get_lang(request)

        # If not connected, show the full value-prop landing
        if gw is None or gw.relay_status not in ("active", "tos_required", "connecting", "error"):
            from celerp.config import ensure_instance_id
            iid = ensure_instance_id()
            return base_shell(
                _section_breadcrumb("Web Access"),
                page_header("Web Access"),
                _value_prop_page(iid, lang=lang),
                title="Web Access - Celerp",
                nav_active="web-access",
                lang=lang,
                request=request,
            )

        # Connected or connecting - show tabs
        tab = request.query_params.get("tab", "status")
        has_team = _has_team_features()

        if tab == "infrastructure" and has_team:
            content = _infrastructure_tab()
        elif tab == "connectors":
            from ui.routes.settings_connectors import connectors_tab_content
            content = await connectors_tab_content(lang)
        else:
            content = Div(_cloud_relay_tab(), _backup_summary_card())
            tab = "status"

        return base_shell(
            _section_breadcrumb("Web Access"),
            page_header("Web Access"),
            _cloud_tabs(tab, has_team_features=has_team, lang=lang),
            content,
            title="Web Access - Celerp",
            nav_active="web-access",
            lang=lang,
            request=request,
        )

    @app.post("/settings/cloud/test-db")
    async def cloud_test_db(request: Request):
        """HTMX: test database connectivity with provided credentials."""
        token = _token(request)
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
            return Span(f"✓ Connected to {name}@{host}:{port}", cls="infra-test-result--ok")
        except asyncio.TimeoutError:
            return Span(t("settings.connection_timed_out_3s"), cls="infra-test-result--err")
        except Exception as exc:
            return Span(f"✗ {type(exc).__name__}: {exc}", cls="infra-test-result--err")

    @app.post("/settings/cloud/test-storage")
    async def cloud_test_storage(request: Request):
        """HTMX: test S3-compatible storage connectivity."""
        token = _token(request)
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
            return Span(f"✗ Save failed: {exc}", cls="infra-test-result--err")

    @app.post("/settings/cloud/restore-db")
    async def cloud_restore_db(request: Request):
        """Restore the previous database URL (GDR undo support)."""
        token = _token(request)
        if not token:
            return P(t("error.unauthorized"), cls="infra-test-result infra-test-result--err")

        try:
            from celerp.config import read_config, write_config
            cfg = read_config()
            if not cfg:
                return Span(t("settings.no_config_file_found"), cls="infra-test-result--err")

            prev_url = cfg.get("database_backup", {}).get("previous_url", "")
            if not prev_url:
                return Span("No previous database URL to restore.", cls="infra-test-result--err")

            current_url = cfg.get("database", {}).get("url", "")
            cfg.setdefault("database_backup", {})["previous_url"] = current_url
            cfg.setdefault("database", {})["url"] = prev_url
            write_config(cfg)

            import subprocess
            subprocess.Popen(["pkill", "-HUP", "-f", "uvicorn"])

            return Span("↩ Restored previous DB URL. Restarting...", cls="infra-test-result--ok")
        except Exception as exc:
            return Span(f"✗ Restore failed: {exc}", cls="infra-test-result--err")


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
        raise RuntimeError("Cannot reach endpoint")
    except httpx.TimeoutException:
        raise RuntimeError("Cannot reach endpoint")

    if r.status_code == 200:
        return f"✓ Connected to bucket '{bucket}'"
    elif r.status_code == 403:
        raise RuntimeError("Invalid credentials (403)")
    elif r.status_code == 404:
        raise RuntimeError("Bucket not found (404)")
    elif r.status_code in (301, 307, 308):
        # Redirect - endpoint reachable but bucket may be in different region
        raise RuntimeError(f"Bucket redirect ({r.status_code}) - check region/endpoint")
    else:
        raise RuntimeError(f"S3 returned {r.status_code}")
