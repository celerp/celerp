# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from celerp import __version__
from celerp.db import get_session
from celerp.services.auth import ROLE_LEVELS, get_current_role, get_current_user
from celerp.services.permissions import require_permission
from celerp.services.system_health import get_system_health

logger = logging.getLogger(__name__)

router = APIRouter()

# Every /settings/* route requires an authenticated user.
settings_router = APIRouter(prefix="/settings", dependencies=[Depends(get_current_user)])


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
    except Exception:
        logger.exception("readiness probe: database not reachable")
        raise HTTPException(503, detail="Service not ready.")


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
    except Exception:
        logger.exception("lb readiness probe: database not reachable")
        raise HTTPException(503, detail="Service not ready.")


@router.get("/__celerp/drain")
async def drain_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Return current drain state. Used by drain polling in SSE generator."""
    from celerp.services.runtime_state import get_runtime_state
    state = await get_runtime_state(session)
    return {"draining": state.get("draining", False)}


@router.get("/health/system", dependencies=[Depends(get_current_user)])
async def system_health() -> dict:
    # Host RAM/CPU/disk figures - authenticated only, never anonymous.
    import asyncio
    return await asyncio.to_thread(get_system_health)


@settings_router.get("/cloud-status")
async def cloud_status() -> dict:
    """Return transport and entitlement independently.

    A missing WS session never means a missing subscription. Durable bearer
    auth reads entitlement when possible; paid instances self-heal only when
    the owner has not explicitly disconnected Cloud.
    """
    from celerp.config import settings
    from celerp.gateway.client import get_client
    from celerp.gateway.state import get_session_token, get_subscription_state
    from celerp.services.cloud_entitlement import subscription_status, sync_existing_entitlement

    gw = get_client()
    relay_status = gw.relay_status if gw else "inactive"
    connected = relay_status in ("active", "tos_required")
    ws_tier, ws_status = get_subscription_state()

    if settings.cloud_disconnected:
        return {
            "connected": False, "relay_status": relay_status,
            "tier": ws_tier or None, "subscription_status": ws_status or None,
            "entitlement_known": False, "entitled": None,
            "last_backup": None, "email_quota": 0, "email_used": 0,
            "email_resets_on": None, "public_url": "",
            "gateway_token_set": bool(await __import__("celerp.services.cloud_entitlement", fromlist=["stored_api_key"]).stored_api_key()),
            "cloud_disconnected": True,
        }

    authoritative = await subscription_status()
    tier = (authoritative or {}).get("tier") or ws_tier or None
    sub_status = (authoritative or {}).get("status") or ws_status or None
    known = authoritative is not None
    entitled = (sub_status in ("active", "trialing") and tier not in (None, "", "free")) if known else None

    if entitled and not connected:
        await sync_existing_entitlement()
        gw = get_client()
        relay_status = gw.relay_status if gw else "inactive"
        connected = relay_status in ("active", "tos_required")

    last_backup = None
    email_quota = 0
    email_used = 0
    email_resets_on = None
    session_token = get_session_token()
    if connected and settings.gateway_instance_id and session_token:
        try:
            import httpx
            from celerp.gateway.state import relay_http_url
            async with httpx.AsyncClient(base_url=relay_http_url(), timeout=3.0) as c:
                r = await c.get("/billing/status", params={
                    "instance_id": settings.gateway_instance_id, "session_token": session_token})
            if r.status_code == 200:
                live = r.json()
                tier = live.get("tier") or tier
                sub_status = live.get("status") or sub_status
                last_backup = live.get("last_backup")
                email_quota = int(live.get("email_quota", 0))
                email_used = int(live.get("email_used", 0))
                email_resets_on = live.get("email_resets_on")
        except Exception:
            pass

    return {
        "connected": connected, "relay_status": relay_status,
        "tier": tier, "subscription_status": sub_status,
        "entitlement_known": known, "entitled": entitled,
        "last_backup": last_backup, "email_quota": email_quota,
        "email_used": email_used, "email_resets_on": email_resets_on,
        "public_url": settings.celerp_public_url,
        "gateway_token_set": bool(await __import__("celerp.services.cloud_entitlement", fromlist=["stored_api_key"]).stored_api_key()),
        "cloud_disconnected": False,
    }

@settings_router.get("/billing-catalog")
async def billing_catalog_api() -> dict:
    """Non-secret display catalog. Failure omits prices rather than fabricating them."""
    from celerp.gateway.state import relay_http_url, with_relay_client
    async def _fetch(client):
        return await client.get(f"{relay_http_url()}/billing/catalog")
    try:
        response = await with_relay_client(5.0, _fetch)
        if response.status_code != 200:
            return {"plans": {}}
        data = response.json()
        return data if isinstance(data, dict) else {"plans": {}}
    except Exception:
        return {"plans": {}}


@settings_router.post("/cloud/billing-portal", dependencies=[require_permission("manage_integrations")])
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


@settings_router.get("/backup-status", dependencies=[require_permission("manage_company_settings")])
async def backup_status() -> dict:
    """Return backup scheduler state: last results and next scheduled run times."""
    from celerp.config import settings
    from celerp.services import backup_scheduler
    from celerp.services.backup_state import is_active
    from celerp.gateway.state import get_subscription_state
    tier, subscription_status = get_subscription_state()
    db = backup_scheduler.last_db_result()
    next_db = backup_scheduler.next_db_run_utc()
    running = bool(backup_scheduler._db_task and not backup_scheduler._db_task.done())
    return {
        "running": running,
        "active": is_active(),
        "gateway_token_set": bool(settings.gateway_token),
        "public_url": settings.celerp_public_url,
        "subscription_tier": tier,
        "subscription_status": subscription_status,
        "enc_ok": bool(settings.backup_encryption_key),
        "enc_key": settings.backup_encryption_key or "",
        "db": {"ok": db.ok, "error": db.error, "size_bytes": db.size_bytes,
               "last_run": db.last_run.isoformat() if db.last_run else None},
        "next_db_utc": next_db.isoformat() if next_db else None,
    }


@settings_router.post("/cloud-disconnect", dependencies=[require_permission("manage_integrations")])
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
    from celerp.config import settings as _s, persist_cloud_settings
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
        # The locked RMW preserves token/public_url while making disconnect sticky.
        # It can wait on a cross-process file lock, so keep that synchronous wait
        # off the sole FastAPI event loop.
        await asyncio.to_thread(persist_cloud_settings, disconnected=True)
    except Exception:
        pass

    return {"disconnected": True}



async def _apply_gateway_token_api(
    token: str, iid: str, public_url: str | None = None,
    tos_version: str | None = None, *, authoritative_public_url: bool = True,
    backup_encryption_key: str | None = None,
    tier: str | None = None, status: str | None = None,
) -> None:
    """Compatibility wrapper around the single entitlement apply service."""
    from celerp.services.cloud_entitlement import apply_activation_state
    await apply_activation_state(
        token, iid, public_url=public_url, tos_version=tos_version,
        authoritative_public_url=authoritative_public_url,
        backup_encryption_key=backup_encryption_key,
        tier=tier, status=status)

@settings_router.post("/cloud-activate", dependencies=[require_permission("manage_integrations")])
async def cloud_activate_api() -> dict:
    """Synchronise Connect using existing credential or approved proof authority."""
    import httpx
    from celerp.config import (
        read_config, settings as _s, ensure_instance_id)
    from celerp.gateway.state import (
        activate_payload, relay_http_url as _rhu, with_relay_client)

    iid = await asyncio.to_thread(ensure_instance_id)
    relay_base = _rhu()
    api_key = _s.gateway_token
    if not api_key:
        try:
            api_key = ((read_config() or {}).get("cloud") or {}).get("token") or ""
        except Exception:
            api_key = ""
    verifier = _s.activation_verifier or ""

    async def _activate(c):
        """Every request here is idempotent, so a reopened connection reruns
        the whole exchange. Returns the activate response, or an error dict."""
        headers = {}
        activation_verifier = verifier or None
        if api_key:
            tok_r = await c.post(
                f"{relay_base}/auth/token", json={"api_key": api_key})
            if tok_r.status_code == 200:
                headers["Authorization"] = (
                    f"Bearer {tok_r.json()['access_token']}")
                activation_verifier = None
            elif tok_r.status_code in (401, 403):
                # A rejected stored key may recover, but UUID authority is
                # allowed only after the relay is positively identified as
                # pre-ratchet. Provider/server ambiguity never weakens auth.
                methods_r = await c.get(f"{relay_base}/auth/methods")
                if methods_r.status_code == 404:
                    secure_activation = False
                elif methods_r.status_code == 200:
                    methods_data = methods_r.json()
                    if not isinstance(methods_data, dict):
                        return {"error": "Could not verify relay activation protocol. Try again."}
                    secure_activation = bool(
                        methods_data.get("secure_activation", False))
                else:
                    return {"error": "Could not verify the stored relay credential. Try again."}
                if secure_activation and not activation_verifier:
                    return {
                        "error": "This computer needs a fresh account verification before it can reconnect. "
                                 "Use the Link Subscription field below.",
                        "instance_id": iid,
                    }
                if not secure_activation:
                    activation_verifier = None
            else:
                return {
                    "error": "Could not verify the stored relay credential. Try again.",
                    "instance_id": iid,
                }
        body = activate_payload(
            iid, activation_verifier=None if headers else activation_verifier)
        return await c.post(
            f"{relay_base}/auth/activate", json=body, headers=headers)

    try:
        r = await with_relay_client(RELAY_CONTROL_TIMEOUT, _activate)
    except (httpx.ConnectError, TimeoutError):
        return {"error": f"Connection to {relay_base} timed out or could not be reached. "                "Check your internet connection or firewall and try again."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Could not reach relay: {type(exc).__name__}: {exc}"}
    if isinstance(r, dict):
        return r

    if r.status_code == 404:
        return {
            "error": f"No active subscription found for this instance ({iid}). "
                     "Complete checkout first, or if you need to move your subscription to this instance, use "
                     "the Link Subscription field below.",
            "instance_id": iid,
        }
    if r.status_code == 402:
        return {"error": r.json().get("detail", "Subscription not active.")}
    if r.status_code == 401:
        return {
            "error": "This computer needs a fresh account verification before it can reconnect. "
                     "Use the Link Subscription field below.",
            "instance_id": iid,
        }
    if r.status_code != 200:
        return {"error": f"Relay returned {r.status_code}: {r.text[:120]}"}

    data = r.json()
    # Authenticated entitlement sync deliberately returns no new credential;
    # proof/legacy activation returns one. In either case persist the credential
    # that is actually authoritative for this instance.
    token = data.get("gateway_token") or api_key
    if not token:
        return {"error": "Relay did not return an activation credential."}
    public_url = data.get("public_url")
    await _apply_gateway_token_api(
        token, iid, public_url=public_url, tos_version=data.get("tos_version"),
        backup_encryption_key=data.get("backup_encryption_key"),
        tier=data.get("tier"), status=data.get("status"))
    gw = __import__("celerp.gateway.client", fromlist=["get_client"]).get_client()
    return {
        "connected": True,
        "relay_status": gw.relay_status if gw else "connecting",
        "public_url": public_url or "",
        "instance_id": iid,
    }


# ── Existing-install partner claim ───────────────────────────────────────────
# An owner/admin previews and accepts a partner's claim over an already-running
# install. The app stores no relay-authoritative state: resolve is a pure preview,
# accept triggers the relay bind, and the binding arrives back only via the relay's
# commercial_updated push (handled by the gateway consumer). This path never touches
# gateway_token, keeping the one-time claim disjoint from the live-session credential.

_CLAIM_TOKEN_MAX = 512


def _validate_claim_token(raw: object) -> tuple[str | None, dict | None]:
    """Function-boundary validation for a claim token, before any relay call.

    Returns (token, None) when valid, else (None, {"error": ...}) with a neutral
    message. Bounds the length so a repeated resolve cannot push an unbounded
    payload at the relay.
    """
    token = raw.strip() if isinstance(raw, str) else ""
    if not token:
        return None, {"error": "Enter a claim token to continue."}
    if len(token) > _CLAIM_TOKEN_MAX:
        return None, {"error": "That claim token is too long to be valid."}
    return token, None


def _partner_identity(data: object) -> dict | None:
    """Presence/type-check a relay resolve response into a display identity.

    A 200 with missing or wrong-typed fields is treated as could-not-verify, never
    partially rendered or fabricated. The support_url and support_email are each
    sanitised through the shared guard the partner offer uses before either can
    reach an href; a hostile or non-canonical value is dropped to empty rather
    than carried through.
    """
    from celerp.gateway.state import safe_support_email, safe_support_url

    if not isinstance(data, dict):
        return None
    name = data.get("display_name")
    partner_id = data.get("partner_id")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(partner_id, str) or not partner_id.strip():
        return None
    return {
        "display_name": name,
        "partner_id": partner_id,
        "support_email": safe_support_email(data.get("support_email")),
        "support_url": safe_support_url(data.get("support_url")),
    }


RELAY_CONTROL_TIMEOUT = 10.0


async def _post_partner_claim(path: str, body: dict) -> tuple[dict | None, dict | None]:
    """POST to a relay claim endpoint with the instance bearer, degrading honestly.

    Exchanges the instance credential for a short-lived relay bearer on the same
    client before the claim POST, so a connect/timeout during either leg degrades
    through one set of transport branches. Returns (json, None) on a 200 response,
    else (None, {"error": ...}) for a connect/timeout error, a failed exchange or
    any other exception, a 409 (the token is no longer acceptable), or any other
    non-200 status (the parked or unreachable relay INERT case). Callers pass the
    full /partners/... path so the endpoint string lives at the call site.
    """
    import httpx
    from celerp.gateway.state import (
        fetch_relay_bearer, relay_http_url, with_relay_client)

    relay_base = relay_http_url()

    async def _claim(c):
        jwt = await fetch_relay_bearer(c)
        return await c.post(
            f"{relay_base}{path}", json=body,
            headers={"Authorization": f"Bearer {jwt}"})

    try:
        r = await with_relay_client(RELAY_CONTROL_TIMEOUT, _claim)
    except httpx.ConnectError:
        return None, {"error": "Could not reach the relay to verify this claim. Check your internet connection."}
    except httpx.TimeoutException:
        return None, {"error": "Verifying this claim timed out. Try again."}
    except Exception as exc:
        return None, {"error": f"Could not verify this claim: {type(exc).__name__}."}
    if r.status_code == 409:
        return None, {"error": "This claim is no longer available. It may already have been claimed or expired."}
    if r.status_code != 200:
        return None, {"error": "This claim could not be verified. It may be invalid, expired, or already used."}
    return r.json(), None


@settings_router.post("/partner-claim/resolve")
async def partner_claim_resolve(payload: dict, role: str = Depends(get_current_role)) -> dict:
    """Preview the partner identity behind a claim token. Owner/admin only. Binds
    nothing: a resolve leaves the install celerp_direct."""
    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["admin"]:
        raise HTTPException(status_code=403, detail="Only an owner or admin can review a partner claim.")
    from celerp.config import settings

    token, err = _validate_claim_token(payload.get("claim_token"))
    if err:
        return err
    if not settings.gateway_token:
        return {"error": "This installation has no cloud identity, so a partner claim cannot be verified."}
    data, err = await _post_partner_claim("/partners/claims/resolve", {"token": token})
    if err:
        return err
    identity = _partner_identity(data)
    if identity is None:
        return {"error": "This claim could not be verified. It may be invalid, expired, or already used."}
    return identity


@settings_router.post("/partner-claim/accept")
async def partner_claim_accept(payload: dict, role: str = Depends(get_current_role)) -> dict:
    """Accept a partner claim: the relay binds the relationship and pushes the new
    commercial context. Owner/admin only. Accepting a token that is no longer
    acceptable (already claimed or expired) is a relay 409, surfaced as a neutral
    not-available message, never a fabricated success. Never touches gateway_token."""
    if ROLE_LEVELS.get(role, 0) < ROLE_LEVELS["admin"]:
        raise HTTPException(status_code=403, detail="Only an owner or admin can accept a partner claim.")
    from celerp.config import settings

    token, err = _validate_claim_token(payload.get("claim_token"))
    if err:
        return err
    if not settings.gateway_token:
        return {"error": "This installation has no cloud identity, so a partner claim cannot be accepted."}
    data, err = await _post_partner_claim("/partners/claims/accept", {"token": token})
    if err:
        return err
    partner_id = data.get("partner_id") if isinstance(data, dict) else None
    if not isinstance(partner_id, str) or not partner_id.strip():
        return {"error": "This claim could not be accepted. It may be invalid, expired, or already used."}

    # Converge local commercial state synchronously from the authoritative
    # post-accept context the relay returns, so the UI never redirects to a
    # stale celerp_direct view when the WS push is offline or reconnecting. The
    # WS may already have applied the same/newer valid version - that is
    # converged success, not a failure. A malformed returned context must NOT
    # overwrite last-known-good and is surfaced to the caller.
    from celerp.gateway.state import apply_commercial_context_async, get_commercial_context
    ctx = data.get("commercial_context")
    if ctx is not None:
        if await apply_commercial_context_async(ctx) == "rejected":
            return {"error": "This claim was accepted but its details could not be applied. "
                             "Reopen Cloud settings to refresh."}
    return {
        "partner_id": partner_id,
        "commercial_mode": get_commercial_context().get("commercial_mode", "celerp_direct"),
    }


@settings_router.post("/cloud-apply-token", dependencies=[require_permission("manage_integrations")])
async def cloud_apply_token_api(payload: dict) -> dict:
    """Apply a pre-fetched gateway token (reconnect confirmation flow)."""
    from celerp.config import ensure_instance_id

    token = payload.get("gateway_token", "")
    public_url_known = "public_url" in payload
    public_url = payload.get("public_url") or None
    tos_version = payload.get("tos_version") or None
    iid = await asyncio.to_thread(ensure_instance_id)
    if not token:
        return {"error": "gateway_token missing"}
    await _apply_gateway_token_api(
        token, iid, public_url=public_url, tos_version=tos_version,
        authoritative_public_url=public_url_known)
    gw = __import__("celerp.gateway.client", fromlist=["get_client"]).get_client()
    return {"connected": True, "relay_status": gw.relay_status if gw else "connecting", "public_url": public_url or ""}


@settings_router.post("/cloud-accept-tos", dependencies=[require_permission("manage_integrations")])
async def cloud_accept_tos_api() -> dict:
    """Persist TOS acceptance, restart gateway client with new tos_version."""
    import asyncio
    from celerp.config import settings as _s, persist_cloud_settings
    from celerp.gateway import client as _gw

    gw = _gw.get_client()
    tos_version = gw.required_tos_version if gw is not None else ""

    try:
        await asyncio.to_thread(persist_cloud_settings, tos_version=tos_version)
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


@settings_router.get("/cloud-instance-id")
async def cloud_instance_id() -> dict:
    """Return the canonical instance_id from the API process."""
    from celerp.config import ensure_instance_id
    return {"instance_id": await asyncio.to_thread(ensure_instance_id)}


RELAY_ACCOUNT_METHODS_TIMEOUT = 6.0


@settings_router.get("/account-methods")
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
    iid = await asyncio.to_thread(ensure_instance_id)
    google = False
    free_email_quota = 0
    secure_activation = False
    needs_activation_challenge = False
    start_url = f"{relay_base}/auth/google/start?instance_id={iid}"

    async def _relay_phase():
        nonlocal google, free_email_quota, secure_activation
        nonlocal needs_activation_challenge, start_url
        async with httpx.AsyncClient(timeout=RELAY_ACCOUNT_METHODS_TIMEOUT) as c:
            r = await c.get(f"{relay_base}/auth/methods")
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    google = bool(data.get("google"))
                    free_email_quota = int(data.get("free_email_quota") or 0)
                    secure_activation = bool(data.get("secure_activation", False))

            api_key = _s.gateway_token
            if google and not api_key:
                # A Cloud disconnect clears only the in-memory token; the on-disk
                # credential and the relay-side link both survive. Read the persisted
                # token to prove possession rather than calling /auth/activate, which
                # rotates the relay key on every call and would orphan the stored
                # credential the next reconnect relies on.
                from celerp.config import read_config
                cfg = read_config() or {}
                api_key = (cfg.get("cloud") or {}).get("token") or ""

            if google and not api_key:
                needs_activation_challenge = True
                return

            if google and api_key:
                tok_r = await c.post(
                    f"{relay_base}/auth/token", json={"api_key": api_key})
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
                elif tok_r.status_code in (401, 403):
                    if secure_activation:
                        needs_activation_challenge = True
                    # A pre-ratchet relay has been explicitly identified by the
                    # methods response, so its legacy open door remains valid.
                else:
                    google = False
                    start_url = ""

    try:
        await asyncio.wait_for(
            _relay_phase(), timeout=RELAY_ACCOUNT_METHODS_TIMEOUT)
    except Exception:
        google = False
        start_url = ""
        needs_activation_challenge = False

    # Config persistence is deliberately outside the relay deadline. The UI's
    # 15s deadline contains the 6s relay phase plus the config lock's 5s budget.
    if google and needs_activation_challenge:
        try:
            from celerp.config import ensure_activation_verifier, activation_challenge
            verifier = await asyncio.to_thread(ensure_activation_verifier)
            start_url = (
                f"{relay_base}/auth/google/start?instance_id={iid}"
                f"&activation_challenge={activation_challenge(verifier)}")
        except Exception:
            google = False
            start_url = ""

    return {
        "google": google,
        "free_email_quota": free_email_quota,
        "google_start_url": start_url,
    }


RELAY_ACCOUNT_SIGNUP_TIMEOUT = 8.0
RELAY_ACCOUNT_STATUS_TIMEOUT = 8.0


@settings_router.post("/account-signup", dependencies=[require_permission("manage_integrations")])
async def account_signup_api(payload: dict) -> dict:
    """Proxy the magic-link signup request using the API-process instance_id."""
    import httpx
    from celerp.config import (
        ensure_connect_identity, activation_challenge)
    from celerp.gateway.state import relay_http_url as _rhu, with_relay_client
    email = str(payload.get("email", "")).strip()
    if not email:
        return {"error": "Email required."}
    relay_base = _rhu()

    try:
        iid, verifier = await asyncio.to_thread(ensure_connect_identity)
    except Exception as exc:
        return {"error": f"Could not persist account identity: {type(exc).__name__}"}

    async def _signup(c):
        return await c.post(
            f"{relay_base}/auth/signup/request",
            json={
                "email": email, "instance_id": iid,
                "activation_challenge": activation_challenge(verifier),
            })

    try:
        r = await with_relay_client(RELAY_ACCOUNT_SIGNUP_TIMEOUT, _signup)
    except (httpx.HTTPError, TimeoutError):
        return {"error": f"Cannot reach {relay_base} - check your internet connection."}
    if r.status_code == 202:
        try:
            data = r.json()
        except Exception:
            data = {}
        return {
            "sent": True,
            "delivery_pending": bool(
                isinstance(data, dict) and data.get("delivery_pending")),
        }
    try:
        detail = r.json().get("detail", r.text[:120])
    except Exception:
        detail = r.text[:120]
    return {"error": str(detail), "status_code": r.status_code}


@settings_router.get("/account-status")
async def account_status_api() -> dict:
    """Proxy the relay account status for the app's post-sign-in polling.

    Once the instance is activated it holds a permanent API key; exchange it
    for a short-lived JWT (the connectors-catalog idiom) so the relay can
    return the full record to the instance that owns it. Without a key, or
    when the exchange fails, fall back to the unauthenticated GET and its
    masked record - polling keeps working either way."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id
    from celerp.gateway.state import relay_http_url as _rhu, with_relay_client
    relay_base = _rhu()
    try:
        iid = await asyncio.to_thread(ensure_instance_id)
    except Exception:
        return {"error": "unreachable"}

    async def _status(c):
        headers = {}
        api_key = _s.gateway_token
        if api_key:
            tok_r = await c.post(f"{relay_base}/auth/token",
                                 json={"api_key": api_key})
            if tok_r.status_code == 200:
                headers = {"Authorization": f"Bearer {tok_r.json()['access_token']}"}
        if headers:
            return await c.get(f"{relay_base}/auth/account",
                               params={"instance_id": iid},
                               headers=headers)
        return await c.get(f"{relay_base}/auth/account",
                           params={"instance_id": iid})

    try:
        r = await with_relay_client(RELAY_ACCOUNT_STATUS_TIMEOUT, _status)
    except (httpx.HTTPError, TimeoutError):
        return {"error": "unreachable"}
    if r.status_code != 200:
        return {"error": f"status {r.status_code}"}
    data = r.json()
    return data if isinstance(data, dict) else {"error": "unexpected response"}


# True wall-clock bounds for relay claim legs and the post-claim activation.
RELAY_CLAIM_TIMEOUT = 8.0
CLAIM_ACTIVATE_TIMEOUT = 4.0


@contextlib.contextmanager
def _relay_leg(leg: str):
    started = time.monotonic()
    outcome: dict = {"status": None}
    try:
        yield outcome
    finally:
        status = outcome["status"]
        logger.info(
            "relay %s leg: %.2fs %s", leg, time.monotonic() - started,
            f"status {status}" if status is not None else "no response")


async def _activate_after_claim(
    iid: str, relay_base: str, verifier: str, api_key: str = "",
) -> dict | None:
    """One bounded, authority-safe activation attempt after a completed claim."""
    import httpx
    from celerp.gateway.state import activate_payload, with_relay_client

    async def _activate(c):
        headers = {}
        if api_key:
            tok_r = await c.post(
                f"{relay_base}/auth/token", json={"api_key": api_key})
            if tok_r.status_code == 200:
                headers["Authorization"] = (
                    f"Bearer {tok_r.json()['access_token']}")
        with _relay_leg("activate") as leg:
            resp = await c.post(
                f"{relay_base}/auth/activate",
                json=activate_payload(
                    iid, activation_verifier=None if headers else verifier),
                headers=headers,
            )
            leg["status"] = resp.status_code
        return resp

    try:
        resp = await with_relay_client(CLAIM_ACTIVATE_TIMEOUT, _activate)
    except (httpx.HTTPError, TimeoutError):
        return None
    if resp.status_code != 200:
        return None
    data = resp.json()
    token = data.get("gateway_token") or api_key
    if not token:
        return None
    await _apply_gateway_token_api(
        token, iid, public_url=data.get("public_url"),
        tos_version=data.get("tos_version"),
        backup_encryption_key=data.get("backup_encryption_key"),
        tier=data.get("tier"), status=data.get("status"))
    import celerp.gateway.client as _gw_mod
    gw = _gw_mod.get_client()
    return {
        "connected": True,
        "relay_status": gw.relay_status if gw else "connecting",
        "public_url": data.get("public_url") or "",
        "instance_id": iid,
    }


@settings_router.post("/cloud-send-otp", dependencies=[require_permission("manage_integrations")])
async def cloud_send_otp_api(payload: dict) -> dict:
    import httpx
    from celerp.config import (
        ensure_connect_identity, activation_challenge)
    from celerp.services.cloud_entitlement import stored_api_key

    email = str(payload.get("email", "")).strip()
    if not email:
        return {"error": "Email required."}
    try:
        iid, verifier = await asyncio.to_thread(ensure_connect_identity)
    except Exception as exc:
        return {"error": f"Could not persist account identity: {type(exc).__name__}"}
    challenge = activation_challenge(verifier)
    from celerp.gateway.state import relay_http_url as _rhu, with_relay_client
    relay_base = _rhu()
    api_key = await stored_api_key()

    async def _send_otp(c):
        headers = {}
        if api_key:
            from celerp.gateway.state import fetch_relay_bearer
            jwt = await fetch_relay_bearer(c, api_key=api_key)
            headers["Authorization"] = f"Bearer {jwt}"
        with _relay_leg("send-otp") as leg:
            r = await c.post(
                f"{relay_base}/billing/claim/send-otp",
                json={
                    "email": email, "instance_id": iid,
                    "activation_challenge": challenge,
                },
                headers=headers,
            )
            leg["status"] = r.status_code
        return r

    try:
        r = await with_relay_client(RELAY_CLAIM_TIMEOUT, _send_otp)
    except (httpx.ConnectError, TimeoutError):
        return {"error": f"Connection to {relay_base} timed out or could not be reached. "                "Check your internet connection or firewall and try again."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Connection error: {type(exc).__name__}: {exc}"}

    if r.status_code == 200:
        out = {"ok": True, "instance_id": iid}
        try:
            out["delivery_pending"] = bool(r.json().get("delivery_pending"))
        except Exception:
            pass
        return out
    try:
        detail = r.json().get("detail", r.text[:80])
    except Exception:
        detail = r.text[:80]
    return {"error": str(detail), "status_code": r.status_code, "instance_id": iid}


@settings_router.post("/cloud-claim", dependencies=[require_permission("manage_integrations")])
async def cloud_claim_api(payload: dict) -> dict:
    import httpx
    from celerp.config import (
        settings as _s, ensure_connect_identity, activation_challenge)
    from celerp.services.cloud_entitlement import stored_api_key

    email = str(payload.get("email", "")).strip()
    subscription_id = payload.get("subscription_id") or None
    otp_code = payload.get("otp_code") or None
    if not email:
        return {"error": "Email required."}

    try:
        iid, verifier = await asyncio.to_thread(ensure_connect_identity)
    except Exception as exc:
        return {"error": f"Could not persist account identity: {type(exc).__name__}"}
    challenge = activation_challenge(verifier)
    from celerp.gateway.state import relay_http_url as _rhu, with_relay_client
    relay_base = _rhu()
    claim_payload: dict = {
        "email": email, "activation_challenge": challenge}
    api_key = await stored_api_key()
    if subscription_id:
        claim_payload["subscription_id"] = subscription_id
    if otp_code:
        claim_payload["otp_code"] = otp_code

    async def _claim(c):
        headers = {"X-Instance-ID": iid}
        if api_key:
            from celerp.gateway.state import fetch_relay_bearer
            jwt = await fetch_relay_bearer(c, api_key=api_key)
            headers["Authorization"] = f"Bearer {jwt}"
        with _relay_leg("claim") as leg:
            r = await c.post(
                f"{relay_base}/billing/claim",
                json=claim_payload, headers=headers)
            leg["status"] = r.status_code
        return r

    try:
        r = await with_relay_client(RELAY_CLAIM_TIMEOUT, _claim)
    except (httpx.ConnectError, TimeoutError):
        return {"error": (
            f"Connection to {relay_base} timed out before the relay confirmed the link. "
            "Try again, or restart Celerp: if the link already went through, "
            "the saved activation proof recovers it safely on startup.")}
    except httpx.TimeoutException:
        return {"error": (
            f"Connection to {relay_base} timed out before the relay confirmed the link. "
            "Try again, or restart Celerp: if the link already went through, "
            "the saved activation proof recovers it safely on startup.")}
    except Exception as exc:
        return {"error": f"Connection error: {type(exc).__name__}: {exc}"}

    if r.status_code == 401:
        try:
            detail = r.json().get("detail", {})
        except Exception:
            detail = {}
        if isinstance(detail, dict):
            return {
                "otp_error": True, "code": detail.get("code", "otp_invalid"),
                "attempts_left": detail.get("attempts_left", 0), "instance_id": iid}
        return {
            "otp_error": True, "code": str(detail),
            "attempts_left": 0, "instance_id": iid}
    if r.status_code == 400:
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = ""
        if detail == "otp_required":
            return {"otp_required": True, "instance_id": iid}
        return {"error": r.text[:80], "instance_id": iid}
    if r.status_code == 404:
        return {
            "error": "No subscription or free account found for that email. "
                     "Check the address and try again.", "instance_id": iid}
    if r.status_code == 429:
        return {"error": "Too many attempts. Try again in an hour.", "instance_id": iid}
    if r.status_code == 403:
        return {"error": "Email does not match the selected subscription.", "instance_id": iid}
    if r.status_code != 200:
        return {"error": r.text[:80], "instance_id": iid}

    data = r.json()
    if data.get("requires_selection"):
        return {
            "requires_selection": True, "matches": data["matches"],
            "instance_id": iid}

    connected = await _activate_after_claim(
        iid, relay_base, verifier, api_key=api_key)
    if connected:
        return connected
    return {"linked": True, "instance_id": iid}


@settings_router.get("/connectors-catalog", dependencies=[require_permission("manage_integrations")])
async def connectors_catalog_api() -> dict:
    """Proxy relay /api/connectors using a fresh relay JWT (API process only)."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    iid = ensure_instance_id()
    api_key = _s.gateway_token  # this is the permanent API key, not a JWT
    if not api_key:
        return {"error": "Not connected to relay.", "connectors": []}

    from celerp.gateway.state import relay_http_url as _rhu, with_relay_client
    relay_base = _rhu()

    async def _catalog(c):
        # Exchange API key for short-lived JWT
        tok_r = await c.post(f"{relay_base}/auth/token", json={"api_key": api_key})
        if tok_r.status_code != 200:
            return {"error": f"Could not authenticate with relay ({tok_r.status_code}).", "connectors": []}
        jwt = tok_r.json()["access_token"]
        return await c.get(
            f"{relay_base}/api/connectors",
            params={"instance_id": iid},
            headers={"Authorization": f"Bearer {jwt}"},
        )

    try:
        r = await with_relay_client(8.0, _catalog)
    except httpx.ConnectError:
        return {"error": f"Cannot reach {relay_base}.", "connectors": []}
    except httpx.TimeoutException:
        return {"error": "Relay timed out.", "connectors": []}
    except Exception as exc:
        return {"error": str(exc), "connectors": []}
    if isinstance(r, dict):
        return r

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


@settings_router.get("/connectors/{platform}/authorize-url", dependencies=[require_permission("manage_integrations")])
async def connector_authorize_url(platform: str, shop: str = "") -> dict:
    """Get OAuth authorize URL for a connector platform via API process (holds gateway token)."""
    import httpx
    from celerp.config import settings as _s, ensure_instance_id

    api_key = _s.gateway_token
    if not api_key:
        return {"error": "Not connected to relay."}

    from celerp.gateway.state import relay_http_url as _rhu, with_relay_client
    relay_base = _rhu()
    iid = ensure_instance_id()

    async def _authorize(c):
        tok_r = await c.post(f"{relay_base}/auth/token", json={"api_key": api_key})
        if tok_r.status_code != 200:
            return {"error": f"Could not authenticate with relay ({tok_r.status_code})."}
        jwt = tok_r.json()["access_token"]
        params = {"instance_id": iid}
        if shop:
            params["shop"] = shop
        return await c.get(
            f"{relay_base}/oauth/{platform}/authorize",
            params=params,
            headers={"Authorization": f"Bearer {jwt}"},
        )

    try:
        r = await with_relay_client(8.0, _authorize)
    except httpx.ConnectError:
        return {"error": f"Cannot reach relay."}
    except httpx.TimeoutException:
        return {"error": "Relay timed out."}
    except Exception as exc:
        return {"error": str(exc)}
    if isinstance(r, dict):
        return r

    if r.status_code == 200:
        return {"authorize_url": r.json().get("authorize_url", "")}
    try:
        detail = r.json().get("detail", r.text[:120])
    except Exception:
        detail = r.text[:120]
    return {"error": detail}
