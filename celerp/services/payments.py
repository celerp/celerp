# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Online invoice payment via Stripe, brokered by Celerp Connect.

The instance stores no Stripe credentials: it asks Celerp Connect to create a Checkout
Session on the merchant's connected account and to report its status. Payment
surfaces are gated on a "payments enabled" flag; confirmed payments record through
the same path as a manual payment.
"""
from __future__ import annotations

import hashlib
import logging

log = logging.getLogger(__name__)


def payments_enabled() -> bool:
    """Whether online payment is available (cached flag from Celerp Connect).

    Cheap enough for the per-render pay-button gate.
    """
    from celerp.gateway.state import get_feature_flags
    return bool(get_feature_flags().get("payments_enabled"))


async def _cloud_request(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Use durable instance authority; a live WebSocket is not billing authority."""
    from celerp.config import settings
    if settings.cloud_disconnected:
        return None
    from celerp.services.cloud_entitlement import authenticated_request
    try:
        response = await authenticated_request(method, path, total_s=20.0, json=payload)
        if response is None or response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception as exc:
        log.warning("cloud %s %s failed: %s", method, path, exc)
        return None


async def _cloud_get(path: str) -> dict | None:
    return await _cloud_request("GET", path)


async def _cloud_post(path: str, payload: dict) -> dict | None:
    return await _cloud_request("POST", path, payload)


# ── Payment (customer-facing, via the hosted invoice view) ───────────────────

async def create_checkout(*, amount_minor: int, currency: str, description: str,
                          success_url: str, cancel_url: str,
                          company_id: str, entity_id: str,
                          share_token: str) -> dict | None:
    """Ask Cloud to open a Checkout Session on the merchant's connected account.

    Returns {"url": <stripe checkout url>} (redirect the customer there), or None on
    failure. `amount_minor` is the balance due in minor units. `success_url` should
    carry the literal `{CHECKOUT_SESSION_ID}` placeholder.
    """
    return await _cloud_post("/billing/connect/checkout", {
        "amount_minor": amount_minor, "currency": currency, "description": description[:250],
        "success_url": success_url, "cancel_url": cancel_url,
        "company_id": company_id, "entity_id": entity_id,
        "share_token_hash": hashlib.sha256(share_token.encode()).hexdigest(),
    })


async def checkout_status(session_id: str) -> dict | None:
    """Session status for reconcile-on-return.

    Returns {"paid": bool, "reference": <intent id|None>, "amount_minor": int,
    "currency": str} or None.
    """
    return await _cloud_get(f"/billing/connect/checkout/{session_id}")


# ── Connect onboarding (merchant-facing, from Settings → Payments) ───────────

async def connect_start() -> dict | None:
    """Begin Connect OAuth via Cloud. Returns {"url": <stripe oauth url>} to
    redirect the merchant to, or None."""
    return await _cloud_post("/billing/connect/authorize", {})


async def connect_status() -> dict:
    """Authoritative status for the settings page: {"enabled": bool}. Falls back to
    the cached feature flag if Cloud is unreachable."""
    return (await _cloud_get("/billing/connect/status")) or {"enabled": payments_enabled()}


async def disconnect() -> bool:
    """Disconnect the merchant's account via Cloud."""
    return bool(await _cloud_post("/billing/connect/disconnect", {}))


# ── Subscription management (merchant-facing, from Web Access settings) ──────

async def billing_portal_url() -> str | None:
    """Stripe Billing Portal URL for the merchant's Celerp subscription (cancel,
    change card, invoices). None if the relay is unreachable or no billing
    account is linked to this instance."""
    from celerp.services.cloud_entitlement import authenticated_request
    try:
        response = await authenticated_request("POST", "/billing/portal", json={})
    except Exception as exc:
        log.warning("billing portal request failed: %s", exc)
        return None
    if response is None or response.status_code != 200:
        return None
    data = response.json()
    return data.get("portal_url") if isinstance(data, dict) else None
