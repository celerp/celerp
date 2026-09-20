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
) -> bool:
    """Persist authoritative activation first, then converge local runtime."""
    from celerp.config import record_cloud_activation, settings
    from celerp.gateway import client as gateway_client
    from celerp.services import backup_scheduler

    effective_public_url = (
        public_url if authoritative_public_url else
        (settings.celerp_public_url or None))
    effective_backup_key = backup_encryption_key or settings.backup_encryption_key
    if not effective_backup_key and effective_public_url:
        import base64, secrets
        effective_backup_key = base64.b64encode(
            secrets.token_bytes(32)).decode()

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
        existing = gateway_client.get_client()
        if existing is not None:
            await existing.close()
            gateway_client.set_client(None)
        backup_scheduler.stop()
        return True

    settings.gateway_token = token
    settings.celerp_public_url = effective_public_url or ""
    settings.cloud_disconnected = False

    from celerp.gateway import ensure_running, has_active_share
    should_serve = bool(settings.celerp_public_url)
    if not should_serve:
        should_serve = await has_active_share()
    existing = gateway_client.get_client()
    if existing is not None and (
            not existing.is_serving(token)
            or (authoritative_public_url and not should_serve)):
        await existing.close()
        gateway_client.set_client(None)
    if should_serve:
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
    """Synchronise a credential without letting authentication rewrite identity."""
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

