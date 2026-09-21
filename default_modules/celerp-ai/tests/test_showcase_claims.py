# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The showcase replies on the assistant page describe what the assistant does with
receipts and statements. Every claim in that copy maps to a compiled agent
capability or a code path here, so a copy edit that outruns the product fails."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from celerp.ai import batch
from celerp.ai import tools as ai_tools
from celerp_ai import routes as ai_routes
from celerp_ai import ui_routes as ai_ui

from test_helpers import real_agent_app

_EN = Path(__file__).resolve().parents[3] / "ui" / "locales" / "en.json"

_CONFIRM = ("POST", "/conversations/{conversation_id}/confirm")

# (phrase in the reply, what backs it): a capability route the agent can call,
# a route on the assistant's own router that the page posts to, or a code
# symbol whose source carries the behavior.
_BATCH_BILLS_CLAIMS = [
    ("Read 5 of 5 receipts", ("symbol", batch.run_batch, "asyncio.as_completed")),
    ("Proposed 5 draft bills", ("capability", "POST", "/docs")),
    ("lines matched to items", ("capability", "GET", "/items")),
    ("new vendor proposed", ("capability", "POST", "/crm/contacts")),
    ("does not match its line totals", ("symbol", ai_routes._bill_proposals, "add up to")),
    ("Nothing is saved until you confirm", ("route", *_CONFIRM)),
]

_RECONCILE_CLAIMS = [
    ("Imported 48 lines", ("capability", "POST", "/accounting/reconciliation/{session_id}/import-file")),
    ("match recorded payments", ("capability", "POST", "/accounting/reconciliation/{session_id}/auto-match")),
    ("proposed marking them paid", ("capability", "POST", "/docs/{entity_id}/payment")),
    ("proposed new bills", ("capability", "POST", "/docs")),
    ("proposed write-off", ("capability", "POST", "/accounting/reconciliation/{session_id}/write-off")),
    ("card you confirm or dismiss", ("symbol", ai_ui._action_card, 'hx_post="/ai/dismiss-action-ui"')),
    ("Nothing changes until you confirm", ("route", *_CONFIRM)),
]


@pytest.fixture(scope="module")
def surface():
    app = real_agent_app()
    compiled = ai_tools.compile_agent_capabilities(app, {})
    return {
        "capabilities": {(cap["method"], cap["path"]) for cap in compiled.values()},
        "routes": {(m, route.path) for route in ai_routes.router.routes for m in route.methods},
    }


def _copy(key: str) -> str:
    return json.loads(_EN.read_text(encoding="utf-8"))[key]


def _backed(surface: dict, backing: tuple) -> bool:
    kind = backing[0]
    if kind == "capability":
        return (backing[1], backing[2]) in surface["capabilities"]
    if kind == "route":
        return (backing[1], backing[2]) in surface["routes"]
    return backing[2] in inspect.getsource(backing[1])


@pytest.mark.parametrize("key,claims", [
    ("ai.scenario_batch_bills_reply", _BATCH_BILLS_CLAIMS),
    ("ai.scenario_reconcile_reply", _RECONCILE_CLAIMS),
])
def test_showcase_copy_claims_have_code_paths(surface, key, claims):
    text = _copy(key)
    for phrase, backing in claims:
        assert phrase in text, f"{key} no longer says {phrase!r}; update the claims table"
        assert _backed(surface, backing), f"{key} claims {phrase!r} but nothing backs it: {backing}"
