# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp import __version__
from celerp.db import get_session
from celerp.services.auth import get_current_user
from celerp.services.system_health import get_system_health

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    # C2: the version must read the same everywhere - desktop, cloud relay, and pip.
    # The desktop app (Electron) is the authoritative source of the user-facing version,
    # so it passes its app version to the spawned API via CELERP_APP_VERSION; /health
    # reports that when present, else the Python package version (pip installs / dev).
    # This is the version the relay/PC-browser path reads (it has no window.celerp), so it
    # no longer disagrees with the desktop.
    return {
        "status": "ok",
        "version": os.environ.get("CELERP_APP_VERSION") or __version__,
        "install_channel": os.environ.get("CELERP_INSTALL_CHANNEL", "pypi"),
    }


@router.get("/health/ready")
async def readiness(session: AsyncSession = Depends(get_session)) -> dict:
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(503, detail=f"DB not reachable: {e}")


# ── Internal load-balancer probes (bypasses DrainMiddleware) ─────────────────

@router.get("/__celerp/health")
async def lb_health() -> dict:
    """Always 200 - used by load balancer liveness probes. No DB check."""
    return {"status": "ok"}


@router.get("/__celerp/ready")
async def lb_ready(session: AsyncSession = Depends(get_session)) -> dict:
    """503 if DB is unreachable - used by load balancer readiness probes."""
    try:
        await session.execute(text("SELECT 1"))
        return {"status": "ok", "db": "ok"}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(503, detail=f"DB not reachable: {e}")


@router.get("/__celerp/drain")
async def drain_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Return current drain state. Used by drain polling in SSE generator."""
    from celerp.services.runtime_state import get_runtime_state
    state = await get_runtime_state(session)
    return {"draining": state.get("draining", False)}


@router.get("/health/system")
async def system_health() -> dict:
    import asyncio
    return await asyncio.to_thread(get_system_health)


@router.get("/settings/cloud-status")
async def cloud_status() -> dict:
    """Return cloud connection status, tier, last backup date, and email quota."""
    from celerp.config import settings
    from celerp.gateway.client import get_client
    from celerp.gateway.state import get_session_token
    gw = get_client()
    relay_status = gw.relay_status if gw else "inactive"
    # Only report "connected" (green dot) when the tunnel is really up. "connecting"
    # and "error" are transient/failed states — reporting them as connected is the
    # "green but still 502" the user sees while the relay has no live upstream.
    connected = relay_status in ("active", "tos_required")
    if not connected:
        return {"connected": False, "relay_status": relay_status, "tier": None, "last_backup": None, "email_quota": 0, "email_used": 0, "email_resets_on": None, "public_url": settings.celerp_public_url, "gateway_token_set": bool(settings.gateway_token), "cloud_disconnected": settings.cloud_disconnected}

    # Tier comes from the gateway's own WS push (set_subscription_state, on the
    # "subscription_updated" message) whenever that's arrived - it needs no extra
    # round trip and is available as soon as the tunnel is up. last_backup/email
    # quota aren't carried over WS, so those still come from the relay HTTP call.
    from celerp.gateway.state import get_subscription_state
    ws_tier, _ws_status = get_subscription_state()
    tier: str | None = ws_tier or None
    last_backup: str | None = None
    email_quota: int = 0
    email_used: int = 0
    email_resets_on: str | None = None
    try:
        import httpx
        from celerp.gateway.state import relay_http_url as _relay_http_url
        instance_id = settings.gateway_instance_id
        session_token = get_session_token()
        if instance_id and session_token:
            async with httpx.AsyncClient(base_url=_relay_http_url(), timeout=3.0) as c:
                r = await c.get(
                    "/billing/status",
                    params={"instance_id": instance_id, "session_token": session_token},
                )
                if r.status_code == 200:
                    data = r.json()
                    tier = data.get("tier") or tier
                    last_backup = data.get("last_backup")
                    email_quota = int(data.get("email_quota", 0))
                    email_used = int(data.get("email_used", 0))
                    email_resets_on = data.get("email_resets_on")
    except Exception:
        pass

    return {
        "connected": True,
        "relay_status": relay_status,
        "tier": tier,
        "last_backup": last_backup,
        "email_quota": email_quota,
        "email_used": email_used,
        "email_resets_on": email_resets_on,
        "public_url": settings.celerp_public_url,
        "gateway_token_set": bool(settings.gateway_token),
        "cloud_disconnected": settings.cloud_disconnected,
    }


@router.post("/settings/cloud/billing-portal")
async def cloud_billing_portal() -> dict:
    """Create a Stripe Billing Portal session via the relay so the merchant can
    manage their subscription (cancel, change card, download invoices)."""
    from celerp.services.payments import billing_portal_url
    url = await billing_portal_url()
    if not url:
        raise HTTPException(
            status_code=502,
            detail="Could not open subscription management. Check that web access is connected, then try again.")
    return {"portal_url": url}


@router.get("/settings/backup-status")
async def backup_status() -> dict:
    """Return backup scheduler state: last results and next scheduled run times."""
    from celerp.config import settings
    from celerp.services import backup_scheduler
    from celerp.services.backup_state import is_active
    db = backup_scheduler.last_db_result()
    next_db = backup_scheduler.next_db_run_utc()
    running = bool(backup_scheduler._db_task and not backup_scheduler._db_task.done())
    return {
        "running": running,
        "active": is_active(),
        "gateway_token_set": bool(settings.gateway_token),
        "public_url": settings.celerp_public_url,
        "enc_ok": bool(settings.backup_encryption_key),
        "enc_key": settings.backup_encryption_key or "",
        "db": {"ok": db.ok, "error": db.error, "size_bytes": db.size_bytes,
               "last_run": db.last_run.isoformat() if db.last_run else None},
        "next_db_utc": next_db.isoformat() if next_db else None,
    }


@router.post("/settings/cloud-disconnect", dependencies=[Depends(get_current_user)])
async def cloud_disconnect() -> dict:
    """Stop the gateway WebSocket client and record a sticky Cloud disconnect.

    The credential (token, public_url) is PRESERVED in config, only marked
    disconnected: the association is not lost, so reconnecting is a local
    one-click operation with no relay round-trip and no re-entering email
    (see cloud_activate_api). The live credential is cleared in-memory so the
    tunnel drops and share-minting stops at once. Nothing reconnects
    automatically - not on restart, not on a settings visit - until the user
    reconnects from Cloud settings or completes a sign-in.
    """
    from celerp.config import settings as _s, read_config, write_config
    from celerp.gateway import client as _gw
    from celerp.gateway.state import set_session_token as _set_session_token

    gw = _gw.get_client()
    if gw is not None:
        # Must await close() (not stop()): stop() only flips a flag the run loop
        # checks between connections, but the loop is blocked in `async for` on
        # the live socket. close() actually closes the WS, so the relay drops
        # _connections[instance_id] and the public URL stops serving immediately.
        await gw.close()
        _gw.set_client(None)

    _set_session_token("")  # must clear before touching config (gate reads this)
    _s.gateway_token = ""
    _s.celerp_public_url = ""
    _s.cloud_disconnected = True

    try:
        cfg = read_config()
        cloud_cfg = cfg.setdefault("cloud", {})
        # Keep cloud_cfg["token"]/["public_url"] intact - the credential is the
        # association, and preserving it is what makes reconnect one click.
        cloud_cfg["disconnected"] = True
        write_config(cfg)
    except Exception:
        pass

    return {"disconnected": True}



async def _apply_gateway_token_api(token: str, iid: str, public_url: str | None = None, tos_version: str | None = None) -> None:
    """Apply a gateway token in the API process: persist config, start WS client.

    Every path here is user-initiated (settings reconnect, sign-in poll, claim),
    so it also ends a sticky Cloud disconnect."""
    import asyncio
    from celerp.config import read_config, settings as _s, persist_cloud_settings, write_config
    from celerp.gateway import client as _gw

    _s.gateway_token = token
    _s.gateway_instance_id = iid
    if _s.cloud_disconnected:
        _s.cloud_disconnected = False
        try:
            cfg = read_config()
            if cfg.get("cloud", {}).pop("disconnected", None) is not None:
                write_config(cfg)
        except Exception:
            pass
    if public_url:
        _s.celerp_public_url = public_url

    if not _s.backup_encryption_key:
        import base64, secrets as _secrets
        _s.backup_encryption_key = base64.b64encode(_secrets.token_bytes(32)).decode()

    try:
        persist_cloud_settings(
            token=token,
            instance_id=iid,
            public_url=public_url,
            tos_version=tos_version,
            backup_encryption_key=_s.backup_encryption_key,
        )
    except Exception:
        pass

    # A prior client whose credential the relay rejected idles in an error state and
    # never rebuilds itself. The fresh token is persisted above; tear the dead client
    # down so the settings page stops reading its stale error and any rebuild below
    # starts clean. On the free no-share path (no rebuild) this alone returns the
    # tunnel to a neutral inactive state.
    existing = _gw.get_client()
    if existing is not None and not existing.is_serving(token):
        await existing.close()
        _gw.set_client(None)

    # Route through ensure_running() - the single construction site - rather than
    # constructing GatewayClient inline; it applies the same lazy-tunnel gate as
    # boot and auto-activate (paid public_url, or a free instance with a live share).
    from celerp.gateway import ensure_running, has_active_share
    if _s.celerp_public_url or await has_active_share():
        ensure_running()
        gw = _gw.get_client()
        for _ in range(15):
            if gw and gw.relay_status == "active":
                break
            await asyncio.sleep(0.2)

    # Backups are a paid-tier feature; public_url is the paid signal.
    if _s.celerp_public_url and _s.backup_enabled and _s.backup_encryption_key:
        from celerp.services import backup_scheduler
        backup_scheduler.start()


@router.post("/settings/cloud-activate", dependencies=[Depends(get_current_user)])
async def cloud_activate_api() -> dict:
    """Reconnect the relay tunnel. Re-activates through the relay to obtain a FRESH
    credential - activate is keyed on the preserved instance_id, so there is no email
    to re-enter, and it resyncs an install whose stored token the relay has rotated
    away from (a stale token only ever gets rejected on reconnect). When the relay
    cannot be reached the call reports the error and leaves the disconnect state
    untouched: there is nothing to tunnel to offline, and re-applying the preserved
    (possibly rotated-away) token would only re-arm the rejected state it fixes."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id
    from celerp.gateway.state import activate_payload, relay_http_url as _rhu

    iid = ensure_instance_id()
    relay_base = _rhu()

    def _connected(public_url: str | None) -> dict:
        gw = __import__("celerp.gateway.client", fromlist=["get_client"]).get_client()
        return {"connected": True, "relay_status": gw.relay_status if gw else "connecting",
                "public_url": public_url or "", "instance_id": iid}

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{relay_base}/auth/activate", json=activate_payload(iid))
    except httpx.ConnectError:
        return {"error": f"Cannot reach {relay_base} - check your internet connection or firewall."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Could not reach relay: {type(exc).__name__}: {exc}"}

    if r.status_code == 404:
        return {
            "error": f"No active subscription found for this instance ({iid}). "
                     "Complete checkout first, or if you need to move your subscription "
                     "to this instance, use the Link Subscription field below.",
            "instance_id": iid,
        }
    if r.status_code == 402:
        return {"error": r.json().get("detail", "Subscription not active.")}
    if r.status_code != 200:
        return {"error": f"Relay returned {r.status_code}: {r.text[:120]}"}

    data = r.json()
    token = data["gateway_token"]
    public_url = data.get("public_url")
    tos_version = data.get("tos_version")
    reconnect = data.get("reconnect", False)

    # An established install reconnecting from a fresh page (not disconnected) may be
    # switching accounts, so the UI confirms before applying. A sticky-disconnected
    # reconnect is the owner explicitly bringing THIS install back - there is no
    # account switch to confirm, so the freshly activated token is applied directly,
    # which is what resyncs a stored credential the relay had rotated away from.
    if reconnect and not _s.cloud_disconnected:
        return {"reconnect": True, "gateway_token": token, "public_url": public_url, "tos_version": tos_version, "instance_id": iid}

    await _apply_gateway_token_api(token, iid, public_url=public_url, tos_version=tos_version)
    return _connected(public_url)


@router.post("/settings/cloud-apply-token")
async def cloud_apply_token_api(payload: dict) -> dict:
    """Apply a pre-fetched gateway token (reconnect confirmation flow)."""
    from celerp.config import ensure_instance_id

    token = payload.get("gateway_token", "")
    public_url = payload.get("public_url") or None
    tos_version = payload.get("tos_version") or None
    iid = ensure_instance_id()
    if not token:
        return {"error": "gateway_token missing"}
    await _apply_gateway_token_api(token, iid, public_url=public_url, tos_version=tos_version)
    gw = __import__("celerp.gateway.client", fromlist=["get_client"]).get_client()
    return {"connected": True, "relay_status": gw.relay_status if gw else "connecting", "public_url": public_url or ""}


@router.post("/settings/cloud-accept-tos")
async def cloud_accept_tos_api() -> dict:
    """Persist TOS acceptance, restart gateway client with new tos_version."""
    import asyncio
    from celerp.config import settings as _s, read_config, write_config
    from celerp.gateway import client as _gw

    gw = _gw.get_client()
    tos_version = gw.required_tos_version if gw is not None else ""

    try:
        cfg = read_config() or {}
        cloud = cfg.setdefault("cloud", {})
        cloud["tos_version"] = tos_version
        write_config(cfg)
    except Exception:
        pass

    if gw is not None:
        gw.stop()
        _gw.set_client(None)

    new_gw = _gw.GatewayClient(
        gateway_token=_s.gateway_token,
        instance_id=_s.gateway_instance_id,
        gateway_url=_s.gateway_url,
    )
    _gw.set_client(new_gw)
    asyncio.create_task(new_gw.run())
    for _ in range(15):
        if new_gw.relay_status == "active":
            break
        await asyncio.sleep(0.2)

    return {"relay_status": new_gw.relay_status, "public_url": _s.celerp_public_url}


@router.get("/settings/cloud-instance-id")
async def cloud_instance_id() -> dict:
    """Return the canonical instance_id from the API process."""
    from celerp.config import ensure_instance_id
    return {"instance_id": ensure_instance_id()}


@router.get("/settings/account-methods", dependencies=[Depends(get_current_user)])
async def account_methods_api() -> dict:
    """Which optional sign-in methods the relay offers, plus the browser URL for
    the Google flow (started in the system browser, bound to this instance).
    Degrades to email-only when the relay is unreachable - never an error.

    An activated install exchanges its gateway credential for a JWT and asks
    the relay for an owner-initiated start URL: that sign-in may CHANGE which
    account this computer is linked to, which the open door refuses. A
    disconnected install reads its preserved on-disk credential (a Cloud
    disconnect clears only the in-memory token), so the switch also works after
    a disconnect. Fresh installs (nothing linked, no stored token) get the
    open-door URL - they have no link to change. The UI fetches this at click
    time, so the URL is always fresh."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id
    from celerp.gateway.state import relay_http_url as _rhu
    relay_base = _rhu()
    iid = ensure_instance_id()
    google = False
    free_email_quota = 0
    start_url = f"{relay_base}/auth/google/start?instance_id={iid}"
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(f"{relay_base}/auth/methods")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    google = bool(data.get("google"))
                    free_email_quota = int(data.get("free_email_quota") or 0)
            api_key = _s.gateway_token
            if google and not api_key:
                # A Cloud disconnect clears only the in-memory token; the on-disk
                # credential and the relay-side link both survive. Read the persisted
                # token to prove possession rather than calling /auth/activate, which
                # rotates the relay key on every call and would orphan the stored
                # credential the next reconnect relies on. A fresh install has no
                # stored token, so api_key stays empty and the open door below serves it.
                from celerp.config import read_config
                cfg = read_config() or {}
                api_key = (cfg.get("cloud") or {}).get("token") or ""
            if google and api_key:
                tok_r = await c.post(f"{relay_base}/auth/token",
                                     json={"api_key": api_key})
                if tok_r.status_code == 200:
                    su = await c.get(
                        f"{relay_base}/auth/google/start-url",
                        params={"instance_id": iid},
                        headers={"Authorization":
                                 f"Bearer {tok_r.json()['access_token']}"})
                    if su.status_code == 200:
                        su_data = su.json()
                        if isinstance(su_data, dict) and su_data.get("url"):
                            start_url = str(su_data["url"])
    except Exception:
        pass
    return {
        "google": google,
        "free_email_quota": free_email_quota,
        "google_start_url": start_url,
    }


@router.post("/settings/account-signup")
async def account_signup_api(payload: dict) -> dict:
    """Proxy the magic-link signup request using the API-process instance_id."""
    import httpx
    from celerp.config import ensure_instance_id
    from celerp.gateway.state import relay_http_url as _rhu
    email = str(payload.get("email", "")).strip()
    if not email:
        return {"error": "Email required."}
    relay_base = _rhu()
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{relay_base}/auth/signup/request",
                             json={"email": email, "instance_id": ensure_instance_id()})
    except httpx.HTTPError:
        return {"error": f"Cannot reach {relay_base} - check your internet connection."}
    if r.status_code == 202:
        return {"sent": True}
    try:
        detail = r.json().get("detail", r.text[:120])
    except Exception:
        detail = r.text[:120]
    return {"error": str(detail), "status_code": r.status_code}


@router.get("/settings/account-status")
async def account_status_api() -> dict:
    """Proxy the relay account status for the app's post-sign-in polling.

    Once the instance is activated it holds a permanent API key; exchange it
    for a short-lived JWT (the connectors-catalog idiom) so the relay can
    return the full record to the instance that owns it. Without a key, or
    when the exchange fails, fall back to the unauthenticated GET and its
    masked record - polling keeps working either way."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id
    from celerp.gateway.state import relay_http_url as _rhu
    relay_base = _rhu()
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            headers = {}
            api_key = _s.gateway_token
            if api_key:
                tok_r = await c.post(f"{relay_base}/auth/token",
                                     json={"api_key": api_key})
                if tok_r.status_code == 200:
                    headers = {"Authorization": f"Bearer {tok_r.json()['access_token']}"}
            if headers:
                r = await c.get(f"{relay_base}/auth/account",
                                params={"instance_id": ensure_instance_id()},
                                headers=headers)
            else:
                r = await c.get(f"{relay_base}/auth/account",
                                params={"instance_id": ensure_instance_id()})
    except httpx.HTTPError:
        return {"error": "unreachable"}
    if r.status_code != 200:
        return {"error": f"status {r.status_code}"}
    data = r.json()
    return data if isinstance(data, dict) else {"error": "unexpected response"}


@router.post("/settings/cloud-send-otp")
async def cloud_send_otp_api(payload: dict) -> dict:
    """Proxy /billing/claim/send-otp to relay using API-process instance_id."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    email = payload.get("email", "").strip()
    if not email:
        return {"error": "Email required."}

    iid = ensure_instance_id()
    from celerp.gateway.state import relay_http_url as _rhu; relay_base = _rhu()

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{relay_base}/billing/claim/send-otp",
                json={"email": email, "instance_id": iid},
            )
    except httpx.ConnectError:
        return {"error": f"Cannot reach {relay_base} - check your internet connection."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Connection error: {type(exc).__name__}: {exc}"}

    if r.status_code == 200:
        return {"ok": True, "instance_id": iid}
    try:
        detail = r.json().get("detail", r.text[:80])
    except Exception:
        detail = r.text[:80]
    return {"error": str(detail), "status_code": r.status_code, "instance_id": iid}


@router.post("/settings/cloud-claim")
async def cloud_claim_api(payload: dict) -> dict:
    """Proxy /billing/claim to relay using API-process instance_id, then activate.

    If a gateway_token is configured, exchange it at /auth/token for a
    short-lived bearer and attach it to the claim request alongside
    X-Instance-ID. Without a token, the claim is sent with X-Instance-ID and
    the email/OTP payload only.
    """
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    email = payload.get("email", "").strip()
    subscription_id = payload.get("subscription_id") or None
    otp_code = payload.get("otp_code") or None

    if not email:
        return {"error": "Email required."}

    iid = ensure_instance_id()
    from celerp.gateway.state import relay_http_url as _rhu; relay_base = _rhu()

    claim_payload: dict = {"email": email}
    if subscription_id:
        claim_payload["subscription_id"] = subscription_id
    if otp_code:
        claim_payload["otp_code"] = otp_code

    headers = {"X-Instance-ID": iid}
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            api_key = _s.gateway_token
            if api_key:
                tok_r = await c.post(f"{relay_base}/auth/token", json={"api_key": api_key})
                if tok_r.status_code == 200:
                    headers["Authorization"] = f"Bearer {tok_r.json()['access_token']}"
            r = await c.post(
                f"{relay_base}/billing/claim",
                json=claim_payload,
                headers=headers,
            )
    except httpx.ConnectError:
        return {"error": f"Cannot reach {relay_base} - check your internet connection or firewall."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Connection error: {type(exc).__name__}: {exc}"}

    if r.status_code == 401:
        try:
            detail = r.json().get("detail", {})
        except Exception:
            detail = {}
        if isinstance(detail, dict):
            return {"otp_error": True, "code": detail.get("code", "otp_invalid"), "attempts_left": detail.get("attempts_left", 0), "instance_id": iid}
        return {"otp_error": True, "code": str(detail), "attempts_left": 0, "instance_id": iid}

    if r.status_code == 400:
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = ""
        if detail == "otp_required":
            return {"otp_required": True, "instance_id": iid}
        return {"error": r.text[:80], "instance_id": iid}

    if r.status_code == 404:
        return {"error": "No subscription or free account found for that email. Check the address and try again.", "instance_id": iid}
    if r.status_code == 429:
        return {"error": "Too many attempts. Try again in an hour.", "instance_id": iid}
    if r.status_code == 403:
        return {"error": "Email does not match the selected subscription.", "instance_id": iid}
    if r.status_code != 200:
        return {"error": r.text[:80], "instance_id": iid}

    data = r.json()

    if data.get("requires_selection"):
        return {"requires_selection": True, "matches": data["matches"], "instance_id": iid}

    # Claim succeeded — activate immediately (same process, same iid)
    try:
        from celerp.gateway.state import activate_payload
        async with httpx.AsyncClient(timeout=10.0) as ac:
            act_resp = await ac.post(f"{relay_base}/auth/activate", json=activate_payload(iid))
        if act_resp.status_code == 200:
            act_data = act_resp.json()
            token = act_data["gateway_token"]
            await _apply_gateway_token_api(token, iid, public_url=act_data.get("public_url"), tos_version=act_data.get("tos_version"))
            import celerp.gateway.client as _gw_mod
            gw = _gw_mod.get_client()
            return {"connected": True, "relay_status": gw.relay_status if gw else "connecting", "public_url": act_data.get("public_url", ""), "instance_id": iid}
    except Exception:
        pass

    return {"linked": True, "instance_id": iid}


@router.get("/settings/connectors-catalog")
async def connectors_catalog_api() -> dict:
    """Proxy relay /api/connectors using a fresh relay JWT (API process only)."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    iid = ensure_instance_id()
    api_key = _s.gateway_token  # this is the permanent API key, not a JWT
    if not api_key:
        return {"error": "Not connected to relay.", "connectors": []}

    from celerp.gateway.state import relay_http_url as _rhu; relay_base = _rhu()
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            # Exchange API key for short-lived JWT
            tok_r = await c.post(f"{relay_base}/auth/token", json={"api_key": api_key})
            if tok_r.status_code != 200:
                return {"error": f"Could not authenticate with relay ({tok_r.status_code}).", "connectors": []}
            jwt = tok_r.json()["access_token"]

            r = await c.get(
                f"{relay_base}/api/connectors",
                params={"instance_id": iid},
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.ConnectError:
        return {"error": f"Cannot reach {relay_base}.", "connectors": []}
    except httpx.TimeoutException:
        return {"error": "Relay timed out.", "connectors": []}
    except Exception as exc:
        return {"error": str(exc), "connectors": []}

    if r.status_code == 200:
        return {"connectors": r.json().get("connectors", [])}
    if r.status_code == 402:
        # Free accounts reach this page but connectors need a paid plan - show
        # the relay's plain upgrade message, not a bare status code.
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = ""
        return {"error": detail or "Connectors need an active Celerp Connect plan.",
                "needs_plan": True, "connectors": []}
    return {"error": f"Relay returned {r.status_code}.", "connectors": []}


@router.get("/settings/connectors/{platform}/authorize-url")
async def connector_authorize_url(platform: str, shop: str = "") -> dict:
    """Get OAuth authorize URL for a connector platform via API process (holds gateway token)."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    api_key = _s.gateway_token
    if not api_key:
        return {"error": "Not connected to relay."}

    from celerp.gateway.state import relay_http_url as _rhu; relay_base = _rhu()
    iid = ensure_instance_id()

    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            tok_r = await c.post(f"{relay_base}/auth/token", json={"api_key": api_key})
            if tok_r.status_code != 200:
                return {"error": f"Could not authenticate with relay ({tok_r.status_code})."}
            jwt = tok_r.json()["access_token"]

            params = {"instance_id": iid}
            if shop:
                params["shop"] = shop
            r = await c.get(
                f"{relay_base}/oauth/{platform}/authorize",
                params=params,
                headers={"Authorization": f"Bearer {jwt}"},
            )
    except httpx.ConnectError:
        return {"error": f"Cannot reach relay."}
    except httpx.TimeoutException:
        return {"error": "Relay timed out."}
    except Exception as exc:
        return {"error": str(exc)}

    if r.status_code == 200:
        return {"authorize_url": r.json().get("authorize_url", "")}
    try:
        detail = r.json().get("detail", r.text[:120])
    except Exception:
        detail = r.text[:120]
    return {"error": detail}
