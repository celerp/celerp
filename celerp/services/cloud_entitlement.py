# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Authoritative Celerp Connect entitlement and credential synchronization.

Connection state and entitlement state are deliberately separate here. Durable
instance credentials authenticate relay REST reads even when no WebSocket
session exists; only explicit reconnect/sync starts a tunnel.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

RELAY_ENTITLEMENT_TIMEOUT = 6.0

# Rare, short-lived handoffs scheduled when activation occurs inside a proxied
# Web Access request. Strong refs prevent a pending post-response restart from
# being garbage-collected.
_runtime_transition_tasks: set[asyncio.Task] = set()


def _spawn_runtime_transition(coro) -> None:
    task = asyncio.create_task(coro)
    _runtime_transition_tasks.add(task)
    task.add_done_callback(_runtime_transition_tasks.discard)


async def shutdown_runtime_transitions() -> None:
    """Cancel deferred handoffs before application gateway teardown."""
    tasks = list(_runtime_transition_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def stored_api_key() -> str:
    """Current API key, falling back to the preserved on-disk credential."""
    from celerp.config import read_config, settings
    if settings.gateway_token:
        return settings.gateway_token
    try:
        cfg = await asyncio.to_thread(read_config)
        return str(((cfg or {}).get("cloud") or {}).get("token") or "")
    except Exception:
        return ""


async def persisted_api_key() -> str:
    """Return only the credential persisted in [cloud].token."""
    from celerp.config import read_config
    try:
        cfg = await asyncio.to_thread(read_config)
        return str((((cfg or {}).get("cloud") or {}).get("token")) or "")
    except Exception:
        return ""


async def authenticated_request(method: str, path: str, *, total_s: float = RELAY_ENTITLEMENT_TIMEOUT,
                                json: dict | None = None, params: dict | None = None,
                                api_key: str | None = None):
    """One bounded relay REST request authenticated by the durable instance key.

    A pending local verifier owns destination authority. An incumbent key may
    still authenticate ordinary reads, but while that verifier exists a key for
    another instance cannot answer on behalf of the local identity being bound.
    """
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
    """Authoritative subscription state independent of the live WS session."""
    try:
        response = await authenticated_request("GET", "/billing/subscription")
    except Exception as exc:
        log.debug("Relay entitlement read failed: %s", exc)
        return None
    if response is None or response.status_code != 200:
        return None
    data = response.json()
    from celerp.gateway.state import entitlement_snapshot
    return data if entitlement_snapshot(data) is not None else None


async def apply_activation_state(
    token: str, iid: str, *, public_url: str | None = None,
    tos_version: str | None = None,
    backup_encryption_key: str | None = None,
    tier: str | None = None, status: str | None = None,
    connect_entitled: bool,
    feature_flags: dict | None = None,
    authoritative_public_url: bool = True,
    persist_state: bool = True,
    expected_api_key: str | None = None,
    expected_verifier: str | None = None,
    keep_disconnected: bool = False,
) -> bool:
    """Persist authoritative activation first, then converge local runtime."""
    from celerp.config import record_cloud_activation, settings
    from celerp.gateway import client as gateway_client
    from celerp.gateway import shutdown as shutdown_gateway
    from celerp.gateway.state import (
        apply_feature_flags_async, relay_session_headers)
    from celerp.services import backup_scheduler

    if not isinstance(connect_entitled, bool):
        raise ValueError("connect_entitled must be a bool")

    effective_public_url = (
        public_url if authoritative_public_url else
        (settings.celerp_public_url or None))
    effective_backup_key = backup_encryption_key or settings.backup_encryption_key
    if persist_state and not effective_backup_key and effective_public_url:
        import base64, secrets
        effective_backup_key = base64.b64encode(
            secrets.token_bytes(32)).decode()

    if persist_state:
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
    elif settings.cloud_disconnected:
        return False

    settings.gateway_instance_id = iid
    if effective_backup_key:
        settings.backup_encryption_key = effective_backup_key
    if tier:
        from celerp.gateway.state import set_subscription_state
        set_subscription_state(tier, status or "")
    if isinstance(feature_flags, dict):
        await apply_feature_flags_async(feature_flags, persist=persist_state)

    if keep_disconnected:
        settings.gateway_token = ""
        settings.celerp_public_url = ""
        settings.cloud_disconnected = True
        if gateway_client.get_client() is not None:
            await shutdown_gateway()
        backup_scheduler.stop()
        return True

    # Disconnect is the durable ownership intent. It may race after the
    # activation CAS while feature-state persistence is still in flight; never
    # revive runtime after that later explicit decision.
    if settings.cloud_disconnected:
        if gateway_client.get_client() is not None:
            await shutdown_gateway()
        backup_scheduler.stop()
        return False

    settings.gateway_token = token
    settings.celerp_public_url = effective_public_url or ""

    from celerp.gateway import ensure_running, has_active_share
    should_serve = connect_entitled
    if not should_serve:
        should_serve = await has_active_share()

    existing = gateway_client.get_client()
    transport_mismatch = False
    if (authoritative_public_url and existing is not None
            and existing.relay_status == "active"):
        runtime_paid = bool(
            relay_session_headers().get("X-Session-Token", ""))
        transport_mismatch = runtime_paid != connect_entitled

    restart_required = bool(
        existing is not None
        and (
            not existing.is_serving(token)
            or transport_mismatch
            or (authoritative_public_url and not should_serve)
        )
    )
    deferred_restart = False
    if restart_required:
        if existing is not None and existing.is_draining_for_reconfigure():
            deferred_restart = True
        elif existing is not None and existing.has_inflight_proxy_requests():
            # This activation can be executing inside the Web Access request that
            # must carry its own success response. Drain that generation first,
            # then rebuild from the latest persisted settings.
            existing.begin_proxy_drain()

            async def _restart_after_proxy_drain(expected_client) -> None:
                try:
                    await expected_client.wait_for_proxy_idle()
                    if gateway_client.get_client() is not expected_client:
                        return
                    latest_should_serve = bool(settings.celerp_public_url)
                    if not latest_should_serve:
                        try:
                            latest_should_serve = await has_active_share()
                        except Exception:
                            latest_should_serve = should_serve
                            log.warning(
                                "Could not refresh relay serving requirement during handoff; "
                                "using the pre-drain decision",
                                exc_info=True,
                            )
                    await shutdown_gateway()
                    if latest_should_serve:
                        ensure_running()
                except Exception:
                    log.warning(
                        "Deferred gateway entitlement reconfiguration failed",
                        exc_info=True,
                    )
                finally:
                    if gateway_client.get_client() is expected_client:
                        expected_client.end_proxy_drain()

            _spawn_runtime_transition(
                _restart_after_proxy_drain(existing))
            deferred_restart = True
        else:
            # Own teardown through the canonical gateway lifecycle. Closing a
            # client directly and immediately replacing it can let the old run
            # task's finalizer clear the new generation's in-process session token.
            await shutdown_gateway()

    if should_serve and not deferred_restart:
        ensure_running()
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

async def sync_existing_entitlement() -> dict | None:
    """Synchronise a configured credential through authenticated activation.

    A credential already present in durable config may update durable state.
    An environment-only credential converges runtime only and is never promoted
    into local config as a side effect.
    """
    from celerp.config import ensure_instance_id, settings
    from celerp.gateway.state import (
        activate_payload, entitlement_snapshot, fetch_relay_auth,
        is_foreign_relay_identity, relay_http_url, with_relay_client)
    if settings.cloud_disconnected:
        return {"disconnected": True}

    key = await stored_api_key()
    if not key:
        return None
    persisted_key = await persisted_api_key()
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
    snapshot = entitlement_snapshot(data)
    if snapshot is None:
        log.warning("Relay activation returned a malformed entitlement snapshot")
        return None
    connect_entitled, feature_flags = snapshot
    token = data.get("gateway_token") or key
    if not token:
        return None
    persist_state = bool(persisted_key and persisted_key == key)
    expected_key = key if persist_state else None
    try:
        accepted = await apply_activation_state(
            token, target_iid, public_url=data.get("public_url"),
            tos_version=data.get("tos_version"),
            backup_encryption_key=data.get("backup_encryption_key"),
            tier=data.get("tier"), status=data.get("status"),
            connect_entitled=connect_entitled,
            feature_flags=feature_flags,
            persist_state=persist_state,
            expected_api_key=expected_key,
        )
    except Exception as exc:
        log.warning(
            "Relay entitlement sync could not apply local state (%s)",
            type(exc).__name__,
        )
        return None
    return data if accepted and isinstance(data, dict) else None