# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

import os

import pytest

_sfn_available = os.path.isfile(os.path.join(os.path.dirname(__file__), "..", "..", "premium_modules", "celerp-sales-funnel", "celerp_sales_funnel", "__init__.py"))
if not _sfn_available:
    pytest.skip("celerp-sales-funnel not installed", allow_module_level=True)

from celerp_contacts.projections import apply_contact_event
from celerp_sales_funnel.projections import apply_deal_event


def test_crm_contact_flow() -> None:
    state = apply_contact_event({}, "crm.contact.created", {"name": "Bob"})
    assert state["name"] == "Bob"

    state = apply_contact_event(state, "crm.contact.updated", {"fields_changed": {"email": {"old": None, "new": "b@c.com"}}})
    assert state["email"] == "b@c.com"

    state = apply_contact_event(state, "crm.contact.tagged", {"tags": ["vip", "vip"]})
    assert state["tags"] == ["vip"]

    state = apply_contact_event(state, "crm.contact.merged", {"source_contact_ids": ["c1", "c2"]})
    assert state["merged_from"] == ["c1", "c2"]


def test_crm_deal_flow() -> None:
    state = apply_deal_event({}, "crm.deal.created", {"name": "Deal"})
    assert state["status"] == "open"

    state = apply_deal_event(state, "crm.deal.stage_changed", {"new_stage": "proposal"})
    assert state["stage"] == "proposal"

    state = apply_deal_event(state, "crm.deal.won", {"notes": "ok"})
    assert state["status"] == "won"

    state = apply_deal_event(state, "crm.deal.lost", {"reason": "price"})
    assert state["status"] == "lost"


def test_contact_unknown_raises() -> None:
    with pytest.raises(ValueError):
        apply_contact_event({}, "crm.contact.nope", {})


def test_deal_unknown_raises() -> None:
    with pytest.raises(ValueError):
        apply_deal_event({}, "crm.deal.nope", {})
