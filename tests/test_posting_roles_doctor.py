# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The books check lists what posting cannot proceed on without a decision.

A posting account the company needs but has not set, and an older document whose
receivable sits on more than one account, are each reported with what to fix. An older entry posted before lines
recorded their role still settles on the account its history proves.
"""
from __future__ import annotations

import pytest

from celerp.models.projections import Projection
from test_cost_restatement import _state
from test_posting_roles_autoje import _account, _invoice, _pay, _remap, _unmap


async def _doctor(client, auth) -> dict:
    r = await client.post("/admin/doctor?checks=posting_origins", headers=auth["headers"])
    assert r.status_code == 200, r.text
    (result,) = r.json()["results"]
    assert result["check"] == "posting_origins"
    assert (result["auto_fixable"], result["fixed"]) == (False, 0)
    return result


async def _strip_snapshots(session, auth, je_id: str, split: dict[str, float] | None = None) -> None:
    """Make an entry look like one posted before lines recorded their role; ``split``
    spreads its receivable line over several accounts."""
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": je_id},
                            populate_existing=True)
    entries = []
    for e in row.state["entries"]:
        e = {k: v for k, v in e.items() if k != "account_roles"}
        if split and e["account"] == "1120" and e.get("debit"):
            entries += [{**e, "account": code, "debit": amount} for code, amount in split.items()]
            continue
        entries.append(e)
    row.state = {**row.state, "entries": entries}
    await session.commit()


@pytest.mark.asyncio
async def test_a_clean_company_has_no_findings(client, auth):
    await _invoice(client, auth, 100.0)
    result = await _doctor(client, auth)
    assert (result["found"], result["details"]) == (0, [])


@pytest.mark.asyncio
async def test_a_needed_posting_account_that_is_not_set_is_reported(session, client, auth):
    await _unmap(session, auth, "receivable")
    result = await _doctor(client, auth)
    (finding,) = [d for d in result["details"] if d.get("role") == "receivable"]
    assert finding["kind"] == "posting_account"
    assert "receivable" in finding["problem"].lower()
    assert finding["fix"] == "/settings/accounting?tab=posting-accounts"


@pytest.mark.asyncio
async def test_an_older_invoice_on_more_than_one_receivable_account_is_reported_and_not_settled(
        session, client, auth):
    inv = await _invoice(client, auth, 100.0)
    await _account(client, auth, "1121", "asset", "1100")
    await _remap(session, auth, "receivable", "1121")
    await _strip_snapshots(session, auth, f"je:auto:{inv}:fin", split={"1120": 60.0, "1121": 40.0})

    result = await _doctor(client, auth)
    (finding,) = result["details"]
    assert (finding["kind"], finding["entity_id"]) == ("party_origin", inv)
    assert "more than one accounts receivable account (1120, 1121)" in finding["problem"]

    r = await _pay(client, auth, inv, 100.0)
    assert r.status_code == 409, r.text
    assert "more than one accounts receivable account" in r.json()["detail"]


@pytest.mark.asyncio
async def test_an_older_invoice_settles_on_the_account_its_history_proves(session, client, auth):
    inv = await _invoice(client, auth, 100.0)
    await _strip_snapshots(session, auth, f"je:auto:{inv}:fin")
    await _account(client, auth, "1121", "asset", "1100")
    await _remap(session, auth, "receivable", "1121")
    assert (await _doctor(client, auth))["found"] == 0

    r = await _pay(client, auth, inv, 100.0)
    assert r.status_code == 200, r.text
    pay = await _state(session, auth, f"je:auto:{inv}:pay:0")
    assert {e["account"]: e["credit"] for e in pay["entries"] if e.get("credit")} == {"1120": 100.0}
