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
    """Persist sticky disconnect first, then stop all live cloud activity."""
    from celerp.config import settings as _s, set_cloud_disconnected
    from celerp.gateway import shutdown as _gateway_shutdown
    from celerp.services import backup_scheduler

    try:
        await asyncio.to_thread(set_cloud_disconnected, True)
    except Exception as exc:
        return {
            "error": f"Could not save the disconnect state: {type(exc).__name__}",
            "disconnected": False,
        }

    backup_scheduler.stop()
    try:
        await _gateway_shutdown()
    except Exception:
        logger.warning("Relay shutdown failed after durable disconnect",
                       exc_info=True)

    _s.gateway_token = ""
    _s.celerp_public_url = ""
    return {"disconnected": True}

async def _apply_gateway_token_api(
    token: str, iid: str, public_url: str | None = None,
    tos_version: str | None = None, *, authoritative_public_url: bool = True,
    backup_encryption_key: str | None = None,
    tier: str | None = None, status: str | None = None,
    connect_entitled: bool,
    feature_flags: dict | None = None,
    expected_api_key: str | None = None,
    expected_verifier: str | None = None,
    keep_disconnected: bool = False,
) -> bool:
    from celerp.services.cloud_entitlement import apply_activation_state
    return await apply_activation_state(
        token, iid, public_url=public_url, tos_version=tos_version,
        authoritative_public_url=authoritative_public_url,
        backup_encryption_key=backup_encryption_key,
        tier=tier, status=status,
        connect_entitled=connect_entitled,
        feature_flags=feature_flags,
        expected_api_key=expected_api_key,
        expected_verifier=expected_verifier,
        keep_disconnected=keep_disconnected,
    )

@settings_router.post("/cloud-activate", dependencies=[require_permission("manage_integrations")])
async def cloud_activate_api(payload: dict | None = None) -> dict:
    """Synchronise Connect using explicit authority and activation intent."""
    import httpx
    from celerp.config import (
        ensure_instance_id, set_cloud_disconnected, settings as _s)
    from celerp.gateway.state import (
        RelayCredentialError, activate_payload, fetch_relay_auth,
        relay_http_url as _rhu, with_relay_client)
    from celerp.services.cloud_entitlement import (
        persisted_api_key, stored_api_key)

    request = payload or {}
    intent = str(request.get("intent") or (
        "connect" if request.get("explicit") else "background"))
    if intent not in {"background", "connect", "account"}:
        intent = "background"

    was_disconnected = bool(_s.cloud_disconnected)
    if intent == "connect":
        try:
            await asyncio.to_thread(set_cloud_disconnected, False)
        except Exception as exc:
            return {"error": f"Could not save reconnect state: {type(exc).__name__}"}
    elif intent == "background" and was_disconnected:
        return {
            "error": "Cloud is explicitly disconnected.",
            "disconnected": True,
            "instance_id": await asyncio.to_thread(ensure_instance_id),
        }

    local_iid = await asyncio.to_thread(ensure_instance_id)
    relay_base = _rhu()
    api_key = await stored_api_key()
    persisted_key = await persisted_api_key()
    verifier = _s.activation_verifier or ""
    authority = {"kind": "none"}
    target = {"iid": local_iid}

    async def _verifier_activate(c):
        authority["kind"] = "verifier"
        target["iid"] = local_iid
        return await c.post(
            f"{relay_base}/auth/activate",
            json=activate_payload(
                local_iid, activation_verifier=verifier),
            headers={},
        )

    async def _activate(c):
        if api_key:
            try:
                jwt, authenticated_iid = await fetch_relay_auth(
                    c, api_key=api_key)
            except RelayCredentialError as exc:
                if exc.status_code in (401, 403):
                    if verifier:
                        return await _verifier_activate(c)
                    return {
                        "error": "This computer needs a fresh account verification before it can reconnect. "
                                 "Use the Link Subscription field below.",
                        "instance_id": local_iid,
                    }
                return {"error": "Could not verify the stored relay credential. Try again."}

            if authenticated_iid and authenticated_iid != local_iid and verifier:
                return await _verifier_activate(c)

            authority["kind"] = "credential"
            target["iid"] = authenticated_iid or local_iid
            response = await c.post(
                f"{relay_base}/auth/activate",
                json=activate_payload(target["iid"]),
                headers={"Authorization": f"Bearer {jwt}"},
            )
            if response.status_code in (401, 403) and verifier:
                return await _verifier_activate(c)
            return response

        if verifier:
            return await _verifier_activate(c)

        return {
            "error": "This computer needs account verification before it can connect. "
                     "Use the Link Subscription field below.",
            "instance_id": local_iid,
        }

    try:
        r = await with_relay_client(RELAY_CONTROL_TIMEOUT, _activate)
    except (httpx.ConnectError, TimeoutError):
        return {"error": f"Connection to {relay_base} timed out or could not be reached. "
                         "Check your internet connection or firewall and try again."}
    except httpx.TimeoutException:
        return {"error": f"Connection to {relay_base} timed out."}
    except Exception as exc:
        return {"error": f"Could not reach relay: {type(exc).__name__}: {exc}"}
    if isinstance(r, dict):
        return r

    iid = target["iid"]
    if r.status_code == 404:
        return {
            "error": f"No active subscription found for this instance ({iid}). "
                     "Complete checkout first, or if you need to move your subscription "
                     "to this instance, use the Link Subscription field below.",
            "instance_id": iid,
        }
    if r.status_code == 402:
        return {"error": r.json().get("detail", "Subscription not active.")}
    if r.status_code in (401, 403):
        return {
            "error": "This computer needs a fresh account verification before it can reconnect. "
                     "Use the Link Subscription field below.",
            "instance_id": local_iid,
        }
    if r.status_code != 200:
        return {"error": f"Relay returned {r.status_code}: {r.text[:120]}"}

    data = r.json()
    from celerp.gateway.state import entitlement_snapshot
    snapshot = entitlement_snapshot(data)
    if snapshot is None:
        return {"error": "Relay returned an invalid entitlement snapshot."}
    connect_entitled, feature_flags = snapshot
    token = data.get("gateway_token") or (
        api_key if authority["kind"] == "credential" else "")
    if not token:
        return {"error": "Relay did not return an activation credential."}

    expected_key = (
        api_key if authority["kind"] == "credential"
        and persisted_key and persisted_key == api_key else None)
    keep_disconnected = intent == "account" and was_disconnected
    accepted = await _apply_gateway_token_api(
        token, iid, public_url=data.get("public_url"),
        tos_version=data.get("tos_version"),
        backup_encryption_key=data.get("backup_encryption_key"),
        tier=data.get("tier"), status=data.get("status"),
        connect_entitled=connect_entitled,
        feature_flags=feature_flags,
        expected_api_key=expected_key,
        expected_verifier=verifier if authority["kind"] == "verifier" else None,
        keep_disconnected=keep_disconnected,
    )
    if not accepted:
        return {
            "error": "Connection state changed while the relay request was in flight. Try again.",
            "instance_id": local_iid,
        }

    if keep_disconnected:
        return {
            "connected": False, "account_bound": True, "disconnected": True,
            "relay_status": "inactive", "public_url": "", "instance_id": iid,
        }

    gw = __import__("celerp.gateway.client", fromlist=["get_client"]).get_client()
    return {
        "connected": True,
        "account_bound": True,
        "relay_status": gw.relay_status if gw else "connecting",
        "public_url": data.get("public_url") or "",
        "instance_id": iid,
    }

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
    from celerp.services.cloud_entitlement import stored_api_key

    api_key = await stored_api_key()
    if not api_key:
        return None, {"error": "This installation has no cloud identity, so a partner claim cannot be used."}
    relay_base = relay_http_url()

    async def _claim(c):
        jwt = await fetch_relay_bearer(c, api_key=api_key)
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
    if not settings.cloud_disconnected:
        return {"error": "Partner claiming is only available while Web Access is disconnected."}
    token, err = _validate_claim_token(payload.get("claim_token"))
    if err:
        return err
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
    if not settings.cloud_disconnected:
        return {"error": "Partner claiming is only available while Web Access is disconnected."}
    token, err = _validate_claim_token(payload.get("claim_token"))
    if err:
        return err
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
    """Compatibility endpoint for trusted local callers.

    Current UI never transports gateway credentials through HTML. A local caller
    may still explicitly apply a credential, using the same durable CAS boundary.
    """
    from celerp.config import ensure_instance_id, set_cloud_disconnected, settings
    from celerp.services.cloud_entitlement import stored_api_key
    token = str(payload.get("gateway_token") or "").strip()
    if not token:
        return {"error": "Missing gateway token."}
    iid = await asyncio.to_thread(ensure_instance_id)
    previous = await stored_api_key()
    try:
        await asyncio.to_thread(set_cloud_disconnected, False)
    except Exception as exc:
        return {"error": f"Could not save reconnect state: {type(exc).__name__}"}
    accepted = await _apply_gateway_token_api(
        token,
        iid,
        public_url=payload.get("public_url"),
        tos_version=payload.get("tos_version"),
        authoritative_public_url="public_url" in payload,
        connect_entitled=bool(
            payload.get("public_url") or settings.celerp_public_url),
        expected_api_key=previous or None,
    )
    if not accepted:
        return {"error": "Connection state changed while reconnecting."}
    import celerp.gateway.client as _gw_mod
    gw = _gw_mod.get_client()
    return {
        "connected": True,
        "relay_status": gw.relay_status if gw else "connecting",
        "public_url": payload.get("public_url") or "",
        "instance_id": iid,
    }


@settings_router.post("/cloud-accept-tos", dependencies=[require_permission("manage_integrations")])
async def cloud_accept_tos_api() -> dict:
    """Persist TOS acceptance, restart gateway client with new tos_version."""
    import asyncio
    from celerp.config import settings as _s, persist_cloud_settings
    from celerp.gateway import ensure_running, shutdown
    from celerp.gateway.client import get_client

    gw = get_client()
    tos_version = gw.required_tos_version if gw is not None else ""

    try:
        await asyncio.to_thread(persist_cloud_settings, tos_version=tos_version)
    except Exception:
        pass

    await shutdown()
    ensure_running()
    new_gw = get_client()
    for _ in range(15):
        if new_gw and new_gw.relay_status == "active":
            break
        await asyncio.sleep(0.2)

    return {
        "relay_status": new_gw.relay_status if new_gw else "inactive",
        "public_url": _s.celerp_public_url,
    }


@settings_router.get("/cloud-instance-id")
async def cloud_instance_id() -> dict:
    """Return the canonical instance_id from the API process."""
    from celerp.config import ensure_instance_id
    return {"instance_id": await asyncio.to_thread(ensure_instance_id)}


RELAY_ACCOUNT_METHODS_TIMEOUT = 6.0


@settings_router.get("/account-methods")
async def account_methods_api() -> dict:
    """Return optional sign-in methods for this local instance.

    An incumbent credential proves only the instance it authenticates; it never
    replaces the durable local destination of an explicit account action.
    """
    from celerp.config import (
        activation_challenge, ensure_activation_verifier, ensure_instance_id)
    from celerp.gateway.state import (
        RelayCredentialError, fetch_relay_auth, is_foreign_relay_identity,
        relay_http_url as _rhu)
    from celerp.services.cloud_entitlement import stored_api_key
    import httpx

    relay_base = _rhu()
    iid = await asyncio.to_thread(ensure_instance_id)
    google = False
    free_email_quota = 0
    secure_activation = False
    needs_activation_challenge = False
    start_url = f"{relay_base}/auth/google/start?instance_id={iid}"

    async def _relay_phase():
        nonlocal google, free_email_quota, secure_activation