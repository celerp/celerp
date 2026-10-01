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
                          company_id: str, entity_id: str,
                          share_token: str) -> dict | None:
    """Ask Cloud to open a Checkout Session on the merchant's connected account.

    Returns {"url": <stripe checkout url>} (redirect the customer there), or None on
    failure. `amount_minor` is the balance due in minor units.
    """
    return await _cloud_post("/billing/connect/checkout", {
        "amount_minor": amount_minor, "currency": currency,
        "description": description[:250],
        "company_id": company_id, "entity_id": entity_id,
        "share_token": share_token,
    })


class PaymentsNotClosed(Exception):
    """Celerp Cloud did not confirm that a company's online payments are closed.

    ``reason`` is "disconnected", "payment_settling", "payment_unrecorded" or "unconfirmed".
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def close_company_payments(company_id: str) -> None:
    """Close a company's online invoice payments at Celerp Cloud for good, before the
    company is deleted: its open payment pages are closed and no new one can start.

    An installation without a Celerp Cloud credential never took online payments and
    returns at once. Otherwise returns only when Cloud confirms this company is closed,
    and raises PaymentsNotClosed for anything else.
    """
    from celerp.config import settings
    from celerp.services import cloud_entitlement
    if not await cloud_entitlement.stored_api_key():
        return
    if settings.cloud_disconnected:
        raise PaymentsNotClosed("disconnected")
    try:
        response = await cloud_entitlement.authenticated_request(
            "POST", "/billing/connect/companies/retire", total_s=30.0, json={"company_id": company_id})
        data = response.json() if response is not None else None
    except Exception as exc:
        log.warning("Closing company payments failed: %s", type(exc).__name__)
        raise PaymentsNotClosed("unconfirmed") from exc
    if not isinstance(data, dict):
        raise PaymentsNotClosed("unconfirmed")
    if response.status_code == 409 and data.get("detail") in ("payment_settling", "payment_unrecorded"):
        raise PaymentsNotClosed(data["detail"])
    if response.status_code != 200 or data != {"retired": True, "company_id": company_id}:
        raise PaymentsNotClosed("unconfirmed")


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
