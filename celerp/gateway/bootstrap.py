# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
# celerp.gateway is a protected BSL internal - 3P modules must not import it.
"""Boot-time partner deployment association.

A partner-packaged install carries a one-time deployment credential. Before the
gateway starts, this seam exchanges that credential for the relay-created
identity (instance_id + api_key) through the explicit relay HTTP endpoint, then
persists the identity so the existing boot gate brings the tunnel up under it.

The relay gateway handshake never consumes the credential, so the association is
recorded only after a positive response from this dedicated call - never as a
side effect of an ordinary hello_ack.
"""

from __future__ import annotations

import logging

from celerp.config import (
    ensure_deployment_nonce,
    record_deployment_association,
    settings,
)

log = logging.getLogger(__name__)


async def associate_partner_deployment() -> bool:
    """Associate a partner-packaged install with its partner via the relay.

    No-op (returns False) unless a deployment credential is set, the install is
    not already associated, and Cloud is not disconnected. On a positive response
    it persists the relay-issued identity, marks the install associated, erases
    the credential, and returns True. Every failure preserves the credential and
    returns False without raising, so a lifespan boot is never crashed.
    """
    credential = (settings.deployment_credential or "").strip()
    if not credential or settings.deployment_associated or settings.cloud_disconnected:
        return False

    from celerp.gateway.state import relay_http_url, relay_post_with_retry

    try:
        # Persist the nonce before any network call: an unpersisted nonce could
        # not be reproduced on a later boot, risking a duplicate association.
        nonce = ensure_deployment_nonce()
    except Exception as exc:
        log.warning(
            "Deployment association skipped: could not persist the nonce (%s).",
            type(exc).__name__,
        )
        return False

    url = f"{relay_http_url()}/partners/deployments/associate"
    body = {"deployment_credential": credential, "deployment_nonce": nonce}

    # The shared helper retries only transient transport failures and quiets
    # httpx's own logger for the duration (the credential travels in this body).
    resp = await relay_post_with_retry(url, body)

    if resp is None:
        log.warning("Deployment association could not reach the relay; will retry next boot.")
        return False

    if resp.status_code != 200:
        if resp.status_code == 403:
            log.warning("Deployment association rejected: invalid or ineligible credential.")
        elif resp.status_code == 409:
            log.warning("Deployment association rejected: instance already owned.")
        else:
            log.warning("Deployment association failed (status %s); will retry next boot.",
                        resp.status_code)
        return False

    try:
        data = resp.json()
    except Exception:
        log.warning("Deployment association returned a malformed response body.")
        return False

    instance_id = (data.get("instance_id") or "").strip() if isinstance(data, dict) else ""
    api_key = (data.get("api_key") or "").strip() if isinstance(data, dict) else ""
    if not instance_id or not api_key:
        log.warning("Deployment association response missing the instance identity.")
        return False

    try:
        record_deployment_association(gateway_token=api_key, instance_id=instance_id)
    except Exception as exc:
        # The durable write failed after a positive response. The credential is
        # preserved and no live-but-unpersisted identity is carried; the persisted
        # nonce makes the next boot's retry resolve to the same association.
        log.warning(
            "Deployment association could not be persisted (%s); will retry next boot.",
            type(exc).__name__,
        )
        return False

    log.info("Partner deployment associated (instance_id=%s).", instance_id)
    return True
