# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The members' club moved in from its old system, then turning funds on, through the API the
pages use.

Initial conditions: the shipped sample migrated in full history into a staged company (no fund
route reaches a staged company, so no fund is asked during the run), plus one draft invoice the
old system held, written by the migration's document sink in the same run. The run is finished
the way a user finishes it.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from migration_support import finalize_run, maker, real_client, real_engine  # noqa: F401
from test_helpers import make_authed_token

pytestmark = pytest.mark.asyncio


async def _migrated_club(real_engine, monkeypatch, tmp_path) -> tuple[dict, str]:
    """The finished migration: (auth for its owner, the migrated draft invoice's id)."""
    from celerp.importers.sample import SAMPLE_ARTIFACT
    from celerp.importers.schema import CIFDocument, CIFLineItem
    from celerp.importers.sinks import SinkContext, sink_for
    from celerp.models.company import Company
    from celerp.models.migration import MigrationRun
    from test_migration_e2e import migrate

    run, rejected = await migrate(real_engine, SAMPLE_ARTIFACT.read_bytes(), SAMPLE_ARTIFACT.name,
                                  {"mode": "full_history"}, monkeypatch, tmp_path)
    assert rejected == []
    draft = CIFDocument(
        source_system="manager_io", source_type="SalesInvoice", source_external_id="club-draft-1",
        doc_type="invoice", status="draft", ref="DRAFT-1", issue_date="2026-01-20", total=Decimal("50.00"),
        amount_paid=Decimal("0"), amount_outstanding=Decimal("50.00"),
        line_items=[CIFLineItem(description="Hall hire", quantity=Decimal("1"), unit_price=Decimal("30.00"),
                                total_price=Decimal("30.00")),
                    CIFLineItem(description="Donation", quantity=Decimal("1"), unit_price=Decimal("20.00"),
                                total_price=Decimal("20.00"))])
    async with maker(real_engine)() as s:
        assert (await s.get(Company, run.company_id)).is_migration_staged
        result = await sink_for("documents").import_batch(SinkContext(
            session=s, company_id=run.company_id, user_id=run.created_by_user_id, run_id=run.id,
            read_attachment=lambda _key: b""), [draft])
        assert (result.created, result.errors) == (1, []), result.errors
        await s.commit()
        doc = result.mappings[0].target_entity_id
        await finalize_run(s, await s.get(MigrationRun, run.id))
    async with maker(real_engine)() as s:
        token = await make_authed_token(s, str(run.created_by_user_id), str(run.company_id), "owner")
    return {"headers": {"Authorization": f"Bearer {token}"}, "company_id": run.company_id,
            "user_id": run.created_by_user_id}, doc


async def test_club_migrated_then_funds_turned_on(real_client, real_engine, monkeypatch, tmp_path):
    """After the move every migrated line is marked as migrated and no fund was asked. With funds
    turned on and a restricted fund added, the migrated draft's Finalize is refused naming both
    lines; with the hall hire line Unrestricted and the donation on the restricted fund it posts
    each line on its choice. With the migrated history locked, a migrated invoice voided
    afterwards is reversed after the lock date on the fund it posted with, Unrestricted, with no
    choice asked, and the restricted fund is untouched. (A migrated journal is not voided by
    hand: like an entry a document wrote, it is undone through what it came from.)"""
    from celerp.models.projections import Projection
    from celerp.services.journal_accounts import fund_account_balances
    from celerp.services.ledger_rows import reversal_state
    from test_funds import _fund, _funds_on
    from test_outside_lines import _choices, _choose, _doc, _entry_ids, _entry_state, _needs_choice

    auth, doc = await _migrated_club(real_engine, monkeypatch, tmp_path)
    state = await _doc(real_client, auth, doc)
    assert state["status"] == "draft" and {li.get("origin") for li in state["line_items"]} == {"migration"}
    assert _choices(state) == (None, [(None, None), (None, None)])
    async with maker(real_engine)() as s:
        migrated = {eid: st for eid, st in (await s.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).all()
            if st.get("origin") == "migration" and st.get("status") == "posted"}
    assert migrated and all(e.get("origin") == "migration" and not e.get("fund_id")
                            for st in migrated.values() for e in st["entries"])

    assert (await _funds_on(real_client, auth)).status_code == 200
    gift = await _fund(real_client, auth, "Building fund")
    async with maker(real_engine)() as session:
        before = await _entry_ids(session, auth)
        r = await real_client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
        _needs_choice(r, state["ref_id"], [1, 2], "migration")
        assert await _entry_ids(session, auth) == before
        hall, donation = (li["line_id"] for li in state["line_items"])
        r = await _choose(real_client, auth, doc, lines={hall: "unrestricted", donation: gift})
        assert r.status_code == 200, r.text
        r = await real_client.post(f"/docs/{doc}/finalize", headers=auth["headers"])
        assert r.status_code == 200, r.text
        entry = await _entry_state(session, auth["company_id"], f"je:auto:{doc}:fin")
        assert sorted((e.get("fund_id") or "", e["credit"]) for e in entry["entries"] if e.get("credit")) == sorted(
            [("", 30.0), (gift, 20.0)])

        inv = next(eid for eid, st in (await session.execute(select(Projection.entity_id, Projection.state).where(
            Projection.company_id == auth["company_id"], Projection.entity_type == "doc"))).all()
            if eid != doc and st.get("doc_type") == "invoice" and st.get("status") == "final"
            and st.get("amount_outstanding") == st.get("total"))
        posted = await _entry_state(session, auth["company_id"], f"je:auto:{inv}:fin")
        receivable = next(e for e in posted["entries"] if e["debit"])
        r = await real_client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": "2026-03-31"})
        assert r.status_code == 200, r.text
        held = await fund_account_balances(session, auth["company_id"])
        r = await real_client.post(f"/docs/{inv}/void", headers=auth["headers"],
                                   json={"reason": "Raised twice in the old system"})
        assert r.status_code == 200, r.text
        voided = await _entry_state(session, auth["company_id"], f"je:auto:{inv}:fin")
        assert voided["status"] == "void" and voided["reversed_on"] > "2026-03-31"
        assert [(e["account"], e.get("fund_id")) for e in reversal_state(f"je:auto:{inv}:fin", voided)["entries"]] == [
            (e["account"], None) for e in posted["entries"]]
        session.expire_all()
        after = await fund_account_balances(session, auth["company_id"])
        account = receivable["account"]
        assert after[None][account] == held[None][account] - Decimal(str(receivable["debit"]))
        assert after[gift] == held[gift]
