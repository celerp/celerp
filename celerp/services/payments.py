# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Online invoice payment via Stripe, brokered by Celerp Connect.

The instance stores no Stripe credentials: it asks Celerp Connect to create a Checkout
Session on the merchant's connected account and to report its status. Payment
surfaces are gated on a "payments enabled" flag; confirmed payments record through
the same path as a manual payment.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def payments_enabled() -> bool:
    """Whether online payment is available (cached flag from Celerp Connect).

    Cheap enough for the per-render pay-button gate.
    """
    from celerp.gateway.state import get_feature_flags
    return bool(get_feature_flags().get("payments_enabled"))


async def _cloud_get(path: str) -> dict | None:
    import httpx

    from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers
    if not get_session_token():
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{relay_http_url()}{path}", headers=relay_session_headers())
        if r.status_code != 200:
            log.debug("cloud GET %s -> %d", path, r.status_code)
            return None
        return r.json()
    except Exception as exc:
        log.warning("cloud GET %s failed: %s", path, exc)
        return None


async def _cloud_post(path: str, payload: dict) -> dict | None:
    import httpx

    from celerp.gateway.state import get_session_token, relay_http_url, relay_session_headers
    if not get_session_token():
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(f"{relay_http_url()}{path}", json=payload, headers=relay_session_headers())
        if r.status_code != 200:
            log.debug("cloud POST %s -> %d", path, r.status_code)
            return None
        return r.json()
    except Exception as exc:
        log.warning("cloud POST %s failed: %s", path, exc)
        return None


# ── Payment (customer-facing, via the hosted invoice view) ───────────────────

async def create_checkout(*, amount_minor: int, currency: str, description: str,
                          success_url: str, cancel_url: str, metadata: dict) -> dict | None:
    """Ask Cloud to open a Checkout Session on the merchant's connected account.

    Returns {"url": <stripe checkout url>} (redirect the customer there), or None on
    failure. `amount_minor` is the balance due in minor units. `success_url` should
    carry the literal `{CHECKOUT_SESSION_ID}` placeholder.
    """
    return await _cloud_post("/billing/connect/checkout", {
        "amount_minor": amount_minor, "currency": currency, "description": description[:250],
        "success_url": success_url, "cancel_url": cancel_url, "metadata": metadata,
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
    result = await _cloud_post("/billing/portal", {})
    return (result or {}).get("portal_url") or None
