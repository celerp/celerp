# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The boot-time chart backfill reaches every company that needs a chart, active or
deactivated, and skips only a company staged for a migration, which it recognises by
the migration service's own staging predicate, never by the company being inactive."""

from __future__ import annotations

import importlib
import uuid
from pathlib import Path

from sqlalchemy import select, text

from migration_support import (
    OWNER_EMAIL,
    auth,
    creator_run,
    finalize_run,
    maker,
    migration_env,  # noqa: F401 - fixture
    real_client,  # noqa: F401 - fixture
    real_engine,  # noqa: F401 - fixture
    staged_run,
)


async def _owner(s):
    from celerp.models.company import User

    user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
    s.add(user)
    await s.flush()
    return user


async def _plain_company(s, name: str, *, active: bool):
    """An ordinary company with no chart, as one created before accounting was enabled."""
    from celerp.models.company import Company

    company = Company(name=name, slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
                      settings={}, is_active=active)
    s.add(company)
    await s.flush()
    return company


async def _backfill(engine) -> set:
    """Run the hook as startup does and return the companies that now hold a chart."""
    from celerp_accounting.models import Account
    from celerp_accounting.routes import backfill_chart_of_accounts_hook

    async with maker(engine)() as s:
        await backfill_chart_of_accounts_hook(session=s)
        await s.commit()
        return set((await s.execute(select(Account.company_id).distinct())).scalars())


async def _staged(engine, company_id) -> bool:
    from celerp.services import migrations

    async with maker(engine)() as s:
        return await migrations.is_company_migration_staged(s, company_id)


async def test_backfill_active_company_without_chart(real_engine):
    """An ordinary active company with no chart is seeded, and is not staged."""
    async with maker(real_engine)() as s:
        company = await _plain_company(s, "Active Co", active=True)
        await s.commit()
    assert await _staged(real_engine, company.id) is False
    assert company.id in await _backfill(real_engine)


async def test_backfill_inactive_company_without_chart(real_engine):
    """An ordinary deactivated company with no chart is seeded, so it works when reactivated."""
    async with maker(real_engine)() as s:
        company = await _plain_company(s, "Paused Co", active=False)
        await s.commit()
    assert await _staged(real_engine, company.id) is False
    assert company.id in await _backfill(real_engine)


async def test_backfill_skips_migration_staged_company(real_engine):
    """A company staged for a migration is never seeded: its chart comes from the imported books."""
    from celerp.services import provisioning

    async with maker(real_engine)() as s:
        staged = await provisioning.provision_migration_company(s, owner=await _owner(s), company_name="Staged Co")
        paused = await _plain_company(s, "Paused Co", active=False)
        await s.commit()
    assert await _staged(real_engine, staged.id) is True
    seeded = await _backfill(real_engine)
    assert staged.id not in seeded
    assert paused.id in seeded


async def test_inactive_company_without_staged_run_is_not_staged(real_engine):
    """Being inactive does not make a company staged; only the migration's own state does."""
    from celerp.services import provisioning

    async with maker(real_engine)() as s:
        paused = await _plain_company(s, "Paused Co", active=False)
        staged = await provisioning.provision_migration_company(s, owner=await _owner(s), company_name="Staged Co")
        await s.commit()
    assert await _staged(real_engine, paused.id) is False
    assert await _staged(real_engine, staged.id) is True


async def test_backfill_uses_the_migration_staging_predicate(real_engine, monkeypatch):
    """The hook asks the migration service's staging predicate about every chartless company
    and skips exactly the companies it names, whatever their active flag says."""
    from celerp.services import migrations

    async with maker(real_engine)() as s:
        active = await _plain_company(s, "Active Co", active=True)
        paused = await _plain_company(s, "Paused Co", active=False)
        held = await _plain_company(s, "Held Co", active=True)
        await s.commit()

    asked = []

    async def predicate(session, company_id):
        asked.append(company_id)
        return company_id == held.id

    monkeypatch.setattr(migrations, "is_company_migration_staged", predicate)
    seeded = await _backfill(real_engine)
    assert {active.id, paused.id, held.id} <= set(asked)
    assert active.id in seeded and paused.id in seeded
    assert held.id not in seeded


async def test_finalized_company_not_classified_staged(real_engine, migration_env):
    """A migration company is staged until its run is finalized, and normal afterwards."""
    from celerp.services import migrations

    run_id, company_id, _ = await staged_run(real_engine)
    assert await _staged(real_engine, company_id) is True
    await migrations.run_migration(run_id)
    assert await _staged(real_engine, company_id) is True
    async with maker(real_engine)() as s:
        await finalize_run(s, await creator_run(s, run_id))
    assert await _staged(real_engine, company_id) is False


async def test_backfilled_inactive_company_reactivated_accounting_works(real_client, real_engine):
    """A company deactivated before accounting reached it is backfilled at startup, and
    once reactivated its owner can post to the ledger."""
    r = await real_client.post("/auth/register", json={
        "company_name": "Paused Co", "email": "paused@example.com", "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    from celerp.services.auth import decode_access_token

    token = r.json()["access_token"]
    headers = auth(token)
    company_id = uuid.UUID(str(decode_access_token(token)["company_id"]))

    # The company as it stood before accounting was enabled: no chart, no bank account.
    async with real_engine.begin() as conn:
        for table in ("bank_accounts", "accounts"):
            await conn.execute(text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": str(company_id)})

    r = await real_client.delete("/companies/me", headers=headers)
    assert r.status_code == 200, r.text
    assert company_id in await _backfill(real_engine)

    r = await real_client.post("/companies/me/reactivate", headers=headers)
    assert r.status_code == 200, r.text
    r = await real_client.post("/accounting/journal-entries", headers=headers, json={
        "ts": "2026-01-15", "memo": "Adjustment", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 80.0, "credit": 0},
                    {"account": "4100", "debit": 0, "credit": 80.0}],
    })
    assert r.status_code == 200, r.text



# ── Every on_modules_ready backfill ──────────────────────────────────────────

PHONE = "+66 2 123 4567"


async def _drop_chart(s, company_id) -> None:
    for table in ("bank_accounts", "accounts"):
        await s.execute(text(f"DELETE FROM {table} WHERE company_id = :c"), {"c": str(company_id)})


async def _has_chart(s, company_id) -> bool:
    from celerp_accounting.models import Account

    return await s.scalar(select(Account.id).where(Account.company_id == company_id).limit(1)) is not None


async def _drop_work_centers(s, company_id) -> None:
    await s.execute(text("DELETE FROM work_centers WHERE company_id = :c"), {"c": str(company_id)})


async def _has_default_work_center(s, company_id) -> bool:
    from celerp.models.company import WorkCenter

    return await s.scalar(select(WorkCenter.id).where(
        WorkCenter.company_id == company_id, WorkCenter.is_default.is_(True)).limit(1)) is not None


async def _self_contact_without_phone(s, company_id) -> None:
    """A self-contact missing the phone the company's settings hold."""
    from celerp.events.engine import emit_event
    from celerp.services.company_lock import locked_company

    sid = f"contact:{uuid.uuid4()}"
    await emit_event(s, company_id=company_id, entity_id=sid, entity_type="contact",
                     event_type="crm.contact.created", data={"name": "Own Co", "contact_type": "both"},
                     actor_id=None, location_id=None, source="test",
                     idempotency_key=f"test:self:{company_id}", metadata_={})
    company = await locked_company(s, company_id)
    company.settings = {**(company.settings or {}), "self_contact_id": sid, "phone": PHONE}


async def _self_contact_has_phone(s, company_id) -> bool:
    from celerp.models.company import Company
    from celerp.models.projections import Projection

    sid = ((await s.get(Company, company_id)).settings or {})["self_contact_id"]
    return (await s.get(Projection, {"company_id": company_id, "entity_id": sid})).state.get("phone") == PHONE


async def _legacy_import(s, company_id) -> None:
    """An earlier import that nothing depends on, with the one-time move not yet run."""
    from celerp.events.engine import emit_event
    from celerp.migrations._data_reconcile import set_meta
    from celerp_docs.received_legacy import LEGACY_RECEIVED_KEY

    await emit_event(s, company_id=company_id, entity_id=f"doc:{uuid.uuid4().hex}", entity_type="doc",
                     event_type="doc.shared_import",
                     data={"doc_type": "invoice", "ref_id": "OLD-1", "company_name": "Old Sender",
                           "total": 50.0, "line_items": []},
                     actor_id=None, location_id=None, source="share_import",
                     idempotency_key=f"share:{uuid.uuid4().hex}:{company_id}", metadata_={})
    conn = await s.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIVED_KEY, ""))


async def _legacy_import_moved(s, company_id) -> bool:
    from celerp.models.ledger import LedgerEntry

    return await s.scalar(select(LedgerEntry.id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == "doc.shared_import").limit(1)) is None


async def _receipt_without_its_record(s, company_id) -> None:
    """A draft bill with goods in, as an earlier release left it: no record of what the receipt found."""
    from celerp.events.engine import emit_event
    from celerp.migrations._data_reconcile import set_meta
    from celerp.models.projections import Projection
    from celerp_docs.legacy_receipts import LEGACY_RECEIPTS_KEY

    bill = f"doc:{uuid.uuid4().hex}"
    for event_type, data in (("doc.created", {"doc_type": "bill", "total": 0.0, "line_items": []}),
                             ("doc.received", {"received_items": [], "location_id": "loc-1"})):
        await emit_event(s, company_id=company_id, entity_id=bill, entity_type="doc", event_type=event_type,
                         data=data, actor_id=None, location_id=None, source="test",
                         idempotency_key=f"test:{event_type}:{bill}", metadata_={})
    row = await s.get(Projection, {"company_id": company_id, "entity_id": bill})
    row.state = {k: v for k, v in row.state.items() if k != "pre_receipt_status"}
    conn = await s.connection()
    await conn.run_sync(lambda c: set_meta(c, LEGACY_RECEIPTS_KEY, ""))


async def _receipt_recorded(s, company_id) -> bool:
    from celerp.models.projections import Projection

    rows = (await s.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc"))).scalars().all()
    return any(state.get("pre_receipt_status") == "draft" for state in rows)


async def _run_issued_by_older_release(s, company_id) -> None:
    """A production run open from an older release, which issued materials without their value."""
    from celerp.events.engine import emit_event

    order = f"mfg:{uuid.uuid4().hex}"
    for event_type, data in (("mfg.order.created", {"description": "Run", "inputs": [], "outputs": []}),
                             ("mfg.order.started", {}),
                             ("mfg.order.issued", {"items": [], "issued_by": None})):
        await emit_event(s, company_id=company_id, entity_id=order, entity_type="mfg_order", event_type=event_type,
                         data=data, actor_id=None, location_id=None, source="test",
                         idempotency_key=f"test:{event_type}:{order}", metadata_={})


async def _run_settled(s, company_id) -> bool:
    from celerp.models.projections import Projection

    rows = (await s.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "mfg_order"))).scalars().all()
    return bool(rows) and not any(state.get("wip_untracked") for state in rows)


async def _delivered_lot_without_its_product(s, company_id) -> None:
    """Goods a migration recorded an invoice delivering, as an earlier release left them: a sold
    lot of a product that names no product."""
    from celerp.events.engine import emit_event

    product, lot = f"item:{uuid.uuid4()}", f"item:{uuid.uuid4()}"
    for entity_id, event_type, data, metadata, source in (
            (product, "item.created", {"sku": "P", "name": "P", "quantity": 0}, {}, "test"),
            (lot, "item.created", {"sku": "P", "name": "P", "quantity": 1}, {"parent_id": product}, "migration"),
            (lot, "item.fulfilled", {"source_doc_id": f"doc:{uuid.uuid4()}", "quantity_fulfilled": 1,
                                     "fulfilled_by": "migration"}, {}, "migration")):
        await emit_event(s, company_id=company_id, entity_id=entity_id, entity_type="item", event_type=event_type,
                         data=data, actor_id=None, location_id=None, source=source,
                         idempotency_key=f"test:{event_type}:{entity_id}", metadata_=metadata)


async def _delivered_lot_linked(s, company_id) -> bool:
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection

    lots = (await s.execute(select(LedgerEntry.entity_id).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == "item.fulfilled"))).scalars().all()
    rows = (await s.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id.in_(lots)))).scalars().all()
    return bool(rows) and all(state.get("catalog_item_id") for state in rows)


async def _bill_imported_by_earlier_release(s, company_id) -> None:
    """An imported bill as an earlier release left it: its import booked it again on top of
    the opening balances. The two accounts it posted to are removed again afterwards when the
    company did not hold them, so the company's chart is as it was."""
    from celerp.events.engine import emit_event
    from celerp.services import auto_je
    from celerp.services.journal_accounts import add_account, lock_accounts

    accounts = {"5100": ("Cost of goods sold", "expense"), "2110": ("Accounts payable", "liability")}
    added = sorted(set(accounts) - set(await lock_accounts(s, company_id, set(accounts)) or {}))
    for code in added:
        await add_account(s, company_id, code, *accounts[code])
    bill = f"doc:{uuid.uuid4().hex}"
    await emit_event(s, company_id=company_id, entity_id=bill, entity_type="doc", event_type="doc.created",
                     data={"doc_type": "bill", "status": "awaiting_payment", "total": 10.0, "line_items": [
                         {"description": "Service", "quantity": 1, "unit_price": 10.0}]},
                     actor_id=None, location_id=None, source="test", idempotency_key=f"test:doc.created:{bill}",
                     metadata_={auto_je.IMPORTED_SNAPSHOT: True})
    await auto_je._emit_auto_posted_je(
        s, company_id=company_id, user_id=None, je_id=f"je:auto:{bill}:bill",
        idem_create=auto_je.je_idempotency_key(bill, "po.converted_to_bill:0", "c"),
        idem_posted=auto_je.je_idempotency_key(bill, "po.converted_to_bill:0", "p"),
        memo="Imported bill", ts="2026-01-01",
        entries=[{"account": "5100", "debit": 10.0, "credit": 0.0}, {"account": "2110", "debit": 0.0, "credit": 10.0}],
        metadata_={"trigger": "doc.converted_to_bill", "doc_id": bill})
    if added:
        await s.execute(text("DELETE FROM accounts WHERE company_id = :c AND code = ANY(:codes)"),
                        {"c": str(company_id), "codes": added})


async def _imported_bill_corrected(s, company_id) -> bool:
    from celerp.models.projections import Projection

    rows = (await s.execute(select(Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.entity_id.like("je:auto:doc:%:bill")))).scalars().all()
    return bool(rows) and all(state.get("status") == "void" for state in rows)


# Each backfill, with what makes a company need it and whether the backfill reached it.
LIFECYCLE_BACKFILLS = {
    "celerp_accounting.routes:backfill_chart_of_accounts_hook": (_drop_chart, _has_chart),
    "celerp_manufacturing.routes:backfill_default_work_center_hook": (_drop_work_centers, _has_default_work_center),
    "celerp_contacts.migrations:backfill_self_contacts_hook": (_self_contact_without_phone, _self_contact_has_phone),
    "celerp_docs.received_legacy:move_legacy_imports_hook": (_legacy_import, _legacy_import_moved),
    "celerp_docs.legacy_receipts:record_legacy_receipts_hook": (_receipt_without_its_record, _receipt_recorded),
    "celerp_manufacturing.routes:settle_open_runs_hook": (_run_issued_by_older_release, _run_settled),
    "celerp_docs.historical_lots:link_historical_lots_hook": (_delivered_lot_without_its_product, _delivered_lot_linked),
    "celerp_docs.imported_cutover:imported_cutover_hook": (_bill_imported_by_earlier_release, _imported_bill_corrected),
}
# Needs a staged company cannot have: only the migration writes to it, and it never
# emits doc.shared_import, which nothing but imports from before Received wrote.
NOT_REACHABLE_WHEN_STAGED = {"celerp_docs.received_legacy:move_legacy_imports_hook"}


def _declared_lifecycle_backfills() -> set[str]:
    """Every on_modules_ready handler the default modules declare in their manifests."""
    from celerp.modules.loader import read_manifest

    handlers = set()
    for pkg in sorted(p for p in (Path(__file__).resolve().parents[1] / "default_modules").iterdir()
                      if (p / "__init__.py").exists() and p.name.startswith("celerp-")):
        manifest = read_manifest(pkg)
        assert manifest, f"{pkg.name} declares no readable PLUGIN_MANIFEST"
        slot = manifest.get("slots", {}).get("on_modules_ready")
        for entry in (slot if isinstance(slot, list) else [slot] if slot else []):
            handlers.add(entry["handler"])
    return handlers


async def _company_rows(engine, company_id) -> dict:
    """A fingerprint of every row the company owns, across every company-scoped table."""
    from celerp.services.migrations import company_tables

    async with maker(engine)() as s:
        rows = {"companies": await s.scalar(text("SELECT c::text FROM companies c WHERE id = :c"),
                                            {"c": str(company_id)})}
        for table in await company_tables(s):
            rows[table] = await s.scalar(text(
                f'SELECT md5(coalesce(string_agg(t::text, \'|\' ORDER BY t::text), \'\')) '
                f'FROM "{table}" t WHERE company_id::text = :c'), {"c": str(company_id)})
    return rows


async def test_no_lifecycle_backfill_filters_on_is_active_alone(real_engine):
    """Every on_modules_ready backfill a default module declares reaches a deactivated company
    that is not staged, and leaves a company staged for a migration exactly as it was."""
    from celerp.services import provisioning

    declared = _declared_lifecycle_backfills()
    assert declared == set(LIFECYCLE_BACKFILLS), "declare each on_modules_ready backfill's need and coverage here"

    async with maker(real_engine)() as s:
        paused = await _plain_company(s, "Paused Co", active=False)
        staged = await provisioning.provision_migration_company(s, owner=await _owner(s), company_name="Staged Co")
        for handler, (need, _) in sorted(LIFECYCLE_BACKFILLS.items()):
            await need(s, paused.id)
            if handler not in NOT_REACHABLE_WHEN_STAGED:
                await need(s, staged.id)
        await s.commit()
    before = await _company_rows(real_engine, staged.id)

    for handler in sorted(declared):
        module, name = handler.split(":")
        async with maker(real_engine)() as s:
            await getattr(importlib.import_module(module), name)(session=s)
            await s.commit()

    async with maker(real_engine)() as s:
        missed = [h for h, (_, covered) in sorted(LIFECYCLE_BACKFILLS.items()) if not await covered(s, paused.id)]
    assert missed == [], f"backfills that skipped a deactivated company: {missed}"
    assert await _company_rows(real_engine, staged.id) == before
    assert await _staged(real_engine, staged.id) is True
