# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import os

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp import __version__
from celerp.db import get_session
from celerp.services.system_health import get_system_health

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
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
        return {"connected": False, "relay_status": relay_status, "tier": None, "last_backup": None, "email_quota": 0, "email_used": 0, "public_url": settings.celerp_public_url, "gateway_token_set": bool(settings.gateway_token)}

    # Try to fetch relay status from cloud
    tier: str | None = None
    last_backup: str | None = None
    email_quota: int = 0
    email_used: int = 0
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
                    tier = data.get("tier")
                    last_backup = data.get("last_backup")
                    email_quota = int(data.get("email_quota", 0))
                    email_used = int(data.get("email_used", 0))
    except Exception:
        pass

    return {
        "connected": True,
        "relay_status": relay_status,
        "tier": tier,
        "last_backup": last_backup,
        "email_quota": email_quota,
        "email_used": email_used,
        "public_url": settings.celerp_public_url,
        "gateway_token_set": bool(settings.gateway_token),
    }


@router.get("/settings/backup-status")
async def backup_status() -> dict:
    """Return backup scheduler state: last results and next scheduled run times."""
    from celerp.config import settings
    from celerp.services import backup_scheduler
    db = backup_scheduler.last_db_result()
    fl = backup_scheduler.last_file_result()
    next_db = backup_scheduler.next_db_run_utc()
    next_fl = backup_scheduler.next_file_run_utc()
    running = bool(backup_scheduler._db_task and not backup_scheduler._db_task.done())
    return {
        "running": running,
        "gateway_token_set": bool(settings.gateway_token),
        "enc_ok": bool(settings.backup_encryption_key),
        "enc_key": settings.backup_encryption_key or "",
        "db": {"ok": db.ok, "error": db.error, "size_bytes": db.size_bytes,
               "last_run": db.last_run.isoformat() if db.last_run else None},
        "file": {"ok": fl.ok, "error": fl.error, "size_bytes": fl.size_bytes,
                 "last_run": fl.last_run.isoformat() if fl.last_run else None},
        "next_db_utc": next_db.isoformat() if next_db else None,
        "next_file_utc": next_fl.isoformat() if next_fl else None,
    }


@router.get("/settings/email-status")
async def email_status() -> dict:
    """Return whether SMTP and/or gateway are configured for email sending."""
    from celerp.config import settings
    return {
        "smtp_configured": bool(settings.smtp_host),
        "gateway_connected": bool(settings.gateway_token),
    }


@router.post("/settings/cloud-disconnect")
async def cloud_disconnect() -> dict:
    """Stop the gateway WebSocket client and clear credentials from config.

    instance_id is preserved - the relay can re-issue a token on next
    /auth/activate call using the same instance_id.
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

    try:
        cfg = read_config()
        if cfg and "cloud" in cfg:
            cfg["cloud"]["token"] = ""
            cfg["cloud"].pop("public_url", None)
            write_config(cfg)
    except Exception:
        pass

    return {"disconnected": True}



async def _apply_gateway_token_api(token: str, iid: str, public_url: str | None = None, tos_version: str | None = None) -> None:
    """Apply a gateway token in the API process: persist config, start WS client."""
    import asyncio
    from celerp.config import settings as _s, read_config, write_config
    from celerp.gateway import client as _gw

    _s.gateway_token = token
    _s.gateway_instance_id = iid
    if public_url:
        _s.celerp_public_url = public_url

    if not _s.backup_encryption_key:
        import base64, secrets as _secrets
        _s.backup_encryption_key = base64.b64encode(_secrets.token_bytes(32)).decode()

    try:
        cfg = read_config()
        if cfg:
            cloud = cfg.setdefault("cloud", {})
            cloud["token"] = token
            cloud["instance_id"] = iid
            if public_url:
                cloud["public_url"] = public_url
            if tos_version:
                cloud["tos_version"] = tos_version
            if _s.backup_encryption_key:
                cloud["backup_encryption_key"] = _s.backup_encryption_key
            write_config(cfg)
    except Exception:
        pass

    if _gw.get_client() is None:
        gw = _gw.GatewayClient(
            gateway_token=token,
            instance_id=iid,
            gateway_url=_s.gateway_url,
        )
        _gw.set_client(gw)
        asyncio.create_task(gw.run())
        for _ in range(15):
            if gw.relay_status == "active":
                break
            await asyncio.sleep(0.2)

    if _s.backup_enabled and _s.backup_encryption_key:
        from celerp.services import backup_scheduler
        backup_scheduler.start()


@router.post("/settings/cloud-activate")
async def cloud_activate_api() -> dict:
    """Call relay /auth/activate, apply token, start gateway client. Returns status."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    iid = ensure_instance_id()
    from celerp.gateway.state import relay_http_url as _rhu; relay_base = _rhu()

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(f"{relay_base}/auth/activate", json={"instance_id": iid})
    except httpx.ConnectError:
        return {"error": f"Cannot reach {relay_base} - check your internet connection or firewall."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Could not reach relay: {type(exc).__name__}: {exc}"}

    if r.status_code == 404:
        body = ""
        try:
            body = r.json().get("detail", "")
        except Exception:
            body = r.text[:120]
        return {"error": f"No active subscription found for this instance ({iid}). {body} Subscribe first, or link by email below.", "instance_id": iid}
    if r.status_code == 402:
        return {"error": r.json().get("detail", "Subscription not active.")}
    if r.status_code != 200:
        return {"error": f"Relay returned {r.status_code}: {r.text[:120]}"}

    data = r.json()
    token = data["gateway_token"]
    public_url = data.get("public_url")
    tos_version = data.get("tos_version")
    reconnect = data.get("reconnect", False)

    if reconnect:
        return {"reconnect": True, "gateway_token": token, "public_url": public_url, "tos_version": tos_version, "instance_id": iid}

    await _apply_gateway_token_api(token, iid, public_url=public_url, tos_version=tos_version)
    gw = __import__("celerp.gateway.client", fromlist=["get_client"]).get_client()
    return {"connected": True, "relay_status": gw.relay_status if gw else "connecting", "public_url": public_url or "", "instance_id": iid}


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
    """Proxy /billing/claim to relay using API-process instance_id, then activate."""
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

    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                f"{relay_base}/billing/claim",
                json=claim_payload,
                headers={"X-Instance-ID": iid},
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
        return {"error": "No subscription found for that email. Check the address and try again.", "instance_id": iid}
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
        async with httpx.AsyncClient(timeout=10.0) as ac:
            act_resp = await ac.post(f"{relay_base}/auth/activate", json={"instance_id": iid})
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
