# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""celerp-subscriptions UI routes."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def doc_period_badge(doc: dict):
    """The doc_detail_badges slot: a document an agreement made names the period it bills and
    links back to the agreement. Whether that period is still billed by this document is the
    agreement's to say, so the page loads it from the agreement as stored (the doc-note route)."""
    from fasthtml.common import A, Div, P
    from ui.i18n import sentence, t

    eid = doc.get("source_agreement")
    if not eid or not doc.get("service_start") or not doc.get("service_end"):
        return None
    doc_id = doc.get("entity_id") or doc.get("id") or ""
    return Div(
        P(sentence("subscriptions.doc_bills_period", start=doc["service_start"], end=doc["service_end"]), " ",
          A(t("subscriptions.doc_open_agreement"), href=f"/subscriptions/{eid}")),
        Div(hx_get=f"/subscriptions/{eid}/doc-note/{doc_id}", hx_trigger="load", hx_swap="outerHTML"),
        cls="doc-period-badge",
    )


def setup_ui_routes(app) -> None:
    # Import before the list routes, so /subscriptions/import is not read as a subscription id.
    from ui.routes import subscriptions_import
    from ui.routes.subscriptions import setup_routes
    subscriptions_import.setup_routes(app)
    setup_routes(app)
    log.info("celerp-subscriptions: UI routes registered")
