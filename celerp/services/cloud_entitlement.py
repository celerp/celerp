# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Synchronize Celerp Connect entitlement and local state."""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

RELAY_ENTITLEMENT_TIMEOUT = 6.0
RUNTIME_DRAIN_TIMEOUT = 8.0

# Rare, short-lived handoffs scheduled when activation occurs inside a proxied
# Web Access request. Strong refs prevent a pending post-response restart from
# being garbage-collected.
_runtime_transition_tasks: set[asyncio.Task] = set()


def _spawn_runtime_transition(coro) -> None:
    task = asyncio.create_task(coro)
    _runtime_transition_tasks.add(task)
    task.add_done_callback(_runtime_transition_tasks.discard)


async def reconfigure_gateway_runtime(*, restart: bool) -> bool:
    """Converge the current gateway generation without cutting off its response."""
    from celerp.config import settings
    from celerp.gateway import client as gateway_client
    from celerp.gateway import ensure_running, shutdown as shutdown_gateway

    expected = gateway_client.get_client()
    if expected is None:
        if restart and not settings.cloud_disconnected:
            ensure_running()
        return False

    if not expected.has_inflight_proxy_requests():
        await shutdown_gateway()
        if restart and not settings.cloud_disconnected:
            ensure_running()
        return False

    drain_generation = expected.begin_proxy_drain()

    async def _after_response() -> None:
        try:
            try:
                await asyncio.wait_for(
                    expected.wait_for_proxy_idle(),
                    timeout=RUNTIME_DRAIN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                pass
            if not expected.owns_proxy_drain(drain_generation):
                return
            if gateway_client.get_client() is expected:
                await shutdown_gateway()
            if restart and not settings.cloud_disconnected:
                ensure_running()
        except Exception:
            log.warning("Deferred gateway reconfiguration failed", exc_info=True)
        finally:
            expected.end_proxy_drain(drain_generation)

    _spawn_runtime_transition(_after_response())
    return True


async def stored_api_key() -> str:
    """Return the configured Connect credential, if any."""
    from celerp.config import read_config, settings
    if settings.gateway_token:
        return settings.gateway_token
    try:
        cfg = await asyncio.to_thread(read_config)
        return str(((cfg or {}).get("cloud") or {}).get("token") or "")
    except Exception:
        return ""


async def persisted_api_key() -> str:
    """Return the persisted Connect credential, if any."""
    from celerp.config import read_config
    try:
        cfg = await asyncio.to_thread(read_config)
        return str((((cfg or {}).get("cloud") or {}).get("token")) or "")
    except Exception:
        return ""


async def authenticated_request(method: str, path: str, *, total_s: float = RELAY_ENTITLEMENT_TIMEOUT,
                                json: dict | None = None, params: dict | None = None,
                                api_key: str | None = None):
    """Make one bounded authenticated Connect request."""
    from celerp.config import ensure_instance_id, settings
    from celerp.gateway.state import (
        fetch_relay_auth, is_foreign_relay_identity,
        relay_http_url, with_relay_client)
    key = api_key or await stored_api_key()
    if not key:
        return None
    base = relay_http_url()
    local_iid = await asyncio.to_thread(ensure_instance_id)
    pending_verifier = settings.activation_verifier or ""

    async def _op(client):
        jwt, authenticated_iid = await fetch_relay_auth(client, api_key=key)
        if (pending_verifier
                and is_foreign_relay_identity(authenticated_iid, local_iid)):
            return None
        return await client.request(
            method, f"{base}{path}", json=json, params=params,
            headers={"Authorization": f"Bearer {jwt}"})

    return await with_relay_client(total_s, _op)


async def subscription_status() -> dict | None:
    """Return the current Connect subscription state, if available."""
    try:
        response = await authenticated_request("GET", "/billing/subscription")
    except Exception as exc:
        log.debug("Relay entitlement read failed: %s", exc)
        return None
    if response is None or response.status_code != 200:
        return None
    data = response.json()
    return data if isinstance(data, dict) else None


async def apply_activation_state(
    token: str, iid: str, *, public_url: str | None = None,
    tos_version: str | None = None,
    backup_encryption_key: str | None = None,
    tier: str | None = None, status: str | None = None,
    authoritative_public_url: bool = True,
    expected_api_key: str | None = None,
    expected_verifier: str | None = None,
    keep_disconnected: bool = False,
    restart_transport: bool = False,
) -> bool:
    """Persist authoritative activation first, then converge local runtime."""
    from celerp.config import record_cloud_activation, settings
    from celerp.gateway import client as gateway_client
    from celerp.gateway.state import get_subscription_state, relay_session_headers
    from celerp.services import backup_scheduler

    effective_public_url = (
        public_url if authoritative_public_url else
        (settings.celerp_public_url or None))
    effective_backup_key = backup_encryption_key or settings.backup_encryption_key
    if not effective_backup_key and effective_public_url:
        import base64, secrets
        effective_backup_key = base64.b64encode(
            secrets.token_bytes(32)).decode()

    # Capture the pre-activation runtime before set_subscription_state below
    # overwrites it. An active socket may still represent the previous entitlement.
    runtime_tier, _runtime_status = get_subscription_state()

    accepted = await asyncio.to_thread(
        record_cloud_activation, token, iid,
        public_url=effective_public_url,
        tos_version=tos_version,
        backup_encryption_key=effective_backup_key,
        expected_api_key=expected_api_key,
        expected_verifier=expected_verifier,
        keep_disconnected=keep_disconnected,
    )
    if not accepted:
        return False

    settings.gateway_instance_id = iid
    if effective_backup_key:
        settings.backup_encryption_key = effective_backup_key
    if tier:
        from celerp.gateway.state import set_subscription_state
        set_subscription_state(tier, status or "")

    if keep_disconnected:
        settings.gateway_token = ""
        settings.celerp_public_url = ""
        settings.cloud_disconnected = True
        if gateway_client.get_client() is not None:
            await reconfigure_gateway_runtime(restart=False)
        backup_scheduler.stop()
        return True

    settings.gateway_token = token
    settings.celerp_public_url = effective_public_url or ""
    settings.cloud_disconnected = False

    from celerp.gateway import ensure_running, has_active_share
    should_serve = bool(settings.celerp_public_url)
    if not should_serve:
        try:
            should_serve = await has_active_share()
        except Exception:
            log.debug("Active-share lookup failed during entitlement convergence", exc_info=True)
            should_serve = False

    existing = gateway_client.get_client()
    transport_mismatch = False
    if (authoritative_public_url and existing is not None
            and existing.relay_status == "active"):
        runtime_paid = bool(
            relay_session_headers().get("X-Session-Token", ""))
        # /auth/activate returns public_url only when the relay considers this
        # instance Connect-entitled, so this mirrors the server's transport verdict
        # including its bounded past_due grace.
        authoritative_paid = bool(effective_public_url)
        tier_mismatch = bool(
            tier and runtime_tier and tier != runtime_tier)
        transport_mismatch = (
            runtime_paid != authoritative_paid or tier_mismatch)

    restart_required = bool(
        existing is not None
        and (
            restart_transport
            or not existing.uses_token(token)
            or transport_mismatch
            or (authoritative_public_url and not should_serve)
        )
    )
    deferred_restart = False
    if restart_required:
        deferred_restart = await reconfigure_gateway_runtime(restart=should_serve)
    elif should_serve:
        ensure_running()

    if should_serve and not deferred_restart:
        gw = gateway_client.get_client()
        for _ in range(15):
            if gw and gw.relay_status in ("active", "tos_required"):
                break
            await asyncio.sleep(0.2)

    if settings.celerp_public_url and settings.backup_enabled and settings.backup_encryption_key:
        backup_scheduler.start()
    else:
        backup_scheduler.stop()
    return True

async def sync_existing_entitlement(
    *, require_persisted_key: bool = False,
) -> dict | None:
    """Synchronize an existing Connect installation."""
    from celerp.config import ensure_instance_id, settings
    from celerp.gateway.state import (
        activate_payload, fetch_relay_auth, is_foreign_relay_identity,
        relay_http_url, with_relay_client)
    if settings.cloud_disconnected:
        return {"disconnected": True}

    key = await stored_api_key()
    if not key:
        return None
    persisted_key = await persisted_api_key()
    if require_persisted_key and (
            not persisted_key or persisted_key != key):
        return None
    local_iid = await asyncio.to_thread(ensure_instance_id)
    pending_verifier = settings.activation_verifier or ""

    async def _sync(client):
        bearer, authenticated_iid = await fetch_relay_auth(
            client, api_key=key)
        if (pending_verifier
                and is_foreign_relay_identity(authenticated_iid, local_iid)):
            return None
        target_iid = authenticated_iid or local_iid
        response = await client.post(
            f"{relay_http_url()}/auth/activate",
            json=activate_payload(target_iid),
            headers={"Authorization": f"Bearer {bearer}"},
        )
        return response, target_iid

    try:
        result = await with_relay_client(RELAY_ENTITLEMENT_TIMEOUT, _sync)
    except Exception as exc:
        log.debug("Relay entitlement sync failed: %s", exc)
        return None
    if result is None:
        return None
    response, target_iid = result
    if response.status_code != 200:
        return None

    data = response.json()
    token = data.get("gateway_token") or key
    if not token:
        return None
    expected_key = key if persisted_key and persisted_key == key else None
    try:
        accepted = await apply_activation_state(
            token, target_iid, public_url=data.get("public_url"),
            tos_version=data.get("tos_version"),
            backup_encryption_key=data.get("backup_encryption_key"),
            tier=data.get("tier"), status=data.get("status"),
            expected_api_key=expected_key,
        )
    except Exception as exc:
        log.warning(
            "Relay entitlement sync could not apply local state (%s)",
            type(exc).__name__,
        )
        return None
    return data if accepted and isinstance(data, dict) else None

