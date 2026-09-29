# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""End-to-end migrations of the Manager fixtures through the real runner and sinks."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from migration_support import OWNER_EMAIL, count, load_run, maker, real_engine, upload_parts  # noqa: F401

pytestmark = pytest.mark.asyncio


async def migrate(engine, data: bytes, name: str, decisions: dict, monkeypatch, tmp_path):
    """Scan, decide and run one source file into a staged company.

    Returns the finished run and every record a sink rejected, as
    (source_type, source_external_id, message).
    """
    from celerp.config import settings
    from celerp.importers import sinks
    from celerp.models.company import User
    from celerp.services import migration_scan_store as store
    from celerp.services import migrations, provisioning

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    rejected: list[tuple[str, str, str]] = []
    for sink in set(sinks._SINKS.values()):
        async def recording(context, records, _import=sink.import_batch):
            result = await _import(context, records)
            rejected.extend((e.source_type, e.source_external_id, e.message) for e in result.errors)
            return result
        monkeypatch.setattr(sink, "import_batch", recording)
    async with maker(engine)() as s:
        user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
        s.add(user)
        await s.flush()
        owner = ("user", user.id)
        scan = await store.create_scan(upload_parts((name, data)), owner=owner)
        chosen = migrations.validate_decisions(scan, decisions)
        scan = store.save_decisions(scan.token, owner=owner, decisions=chosen)
        company = await provisioning.provision_migration_company(s, owner=user, company_name="Example Books Co")
        run = await migrations.create_run(s, company=company, user=user, scan=scan, decisions=chosen)
        await migrations.request_start(s, run)
        await s.commit()
    await migrations.run_migration(run.id)
    return await load_run(engine, run.id), rejected


# ── Lifecycle hooks and staged companies ──────────────────────────────────────

async def test_chart_backfill_skips_staged_migration_company(real_engine):
    """The boot-time chart backfill seeds active companies only: a staged migration
    company's chart comes from the imported books."""
    from celerp.models.company import Company, User
    from celerp.services import provisioning
    from celerp_accounting.models import Account
    from celerp_accounting.routes import backfill_chart_of_accounts_hook

    async with maker(real_engine)() as s:
        user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
        s.add(user)
        await s.flush()
        staged = await provisioning.provision_migration_company(s, owner=user, company_name="Staged Co")
        active = Company(name="Active Co", slug="active-co", settings={}, is_active=True)
        s.add(active)
        await s.flush()
        await backfill_chart_of_accounts_hook(session=s)
        await s.commit()
        seeded = set((await s.execute(select(Account.company_id).distinct())).scalars())
    assert active.id in seeded
    assert staged.id not in seeded


async def test_default_location_is_settled_once_and_deterministically(real_engine):
    """A company without a default gets exactly one: the oldest location, or the
    standard one when it has none, and asking again changes nothing."""
    from celerp.models.company import Location, User
    from celerp.services import provisioning

    async with maker(real_engine)() as s:
        user = User(email=OWNER_EMAIL, name="Owner", is_install_owner=True)
        s.add(user)
        await s.flush()
        empty = await provisioning.provision_migration_company(s, owner=user, company_name="Empty Co")
        created = await provisioning.ensure_default_location(s, empty.id)
        assert (await provisioning.ensure_default_location(s, empty.id)).id == created.id
        assert created.name == provisioning.DEFAULT_LOCATION_NAME

        stocked = await provisioning.provision_migration_company(s, owner=user, company_name="Stocked Co")
        first = Location(company_id=stocked.id, name="Warehouse A", type="warehouse", is_default=False)
        s.add(first)
        await s.flush()
        s.add(Location(company_id=stocked.id, name="Warehouse B", type="warehouse", is_default=False))
        await s.flush()
        assert (await provisioning.ensure_default_location(s, stocked.id)).id == first.id
        await provisioning.add_missing_required_defaults(s, stocked.id)
        defaults = (await s.execute(
            select(Location).where(Location.company_id == stocked.id, Location.is_default.is_(True))
        )).scalars().all()
        assert [loc.id for loc in defaults] == [first.id]
        await s.commit()
