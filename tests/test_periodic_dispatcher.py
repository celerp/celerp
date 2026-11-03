# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The periodic dispatcher: every registered operation runs for every company that
uses its module, each in its own transaction, and one failing company or operation
never stops the rest. Runs on real PostgreSQL, where each transaction commits."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.models.company import Location

pytestmark = pytest.mark.process

_ROOT = Path(__file__).resolve().parents[1]
_CALLS: list[tuple[str, uuid.UUID, date, int]] = []
_FAIL_FOR: set[uuid.UUID] = set()


async def _record(name: str, session, company_id, today, budget) -> None:
    _CALLS.append((name, company_id, today, budget))
    session.add(Location(id=uuid.uuid4(), company_id=company_id, name=f"periodic-{name}:{uuid.uuid4()}",
                         type="warehouse"))
    await session.flush()
    if name == "a" and company_id in _FAIL_FOR:
        raise RuntimeError("operation failed for this company")


async def _op_a(*, session, company_id, today, budget):
    await _record("a", session, company_id, today, budget)


async def _op_b(*, session, company_id, today, budget):
    await _record("b", session, company_id, today, budget)


@pytest.fixture
def ops(monkeypatch):
    from celerp.modules import slots

    _CALLS.clear()
    _FAIL_FOR.clear()
    monkeypatch.setitem(slots._slots, "periodic_operation", [
        {"handler": f"{__name__}:_op_a", "module": "celerp-subscriptions",
         "_module": "celerp-subscriptions", "_first_party": True},
        {"handler": f"{__name__}:_op_b", "module": "celerp-accounting",
         "_module": "celerp-accounting", "_first_party": True},
    ])
    return _CALLS


async def _companies(factory):
    from test_fresh_authority_races_pg import _seed

    subs = await _seed(factory, modules=["celerp-subscriptions"])
    books = await _seed(factory, modules=["celerp-accounting"])
    return subs["company_id"], books["company_id"]


async def _written(factory, company_id) -> list[str]:
    """The operations that committed a write for the company, one entry per write (location
    names are unique per company, so each write carries its own suffix)."""
    async with factory() as s:
        return sorted(name.split(":")[0] for name in (await s.execute(select(Location.name).where(
            Location.company_id == company_id, Location.name.like("periodic-%")))).scalars().all())


async def test_dispatcher_runs_each_operation_per_using_company(committed_engine, ops):
    from celerp.modules.registry import dependency_closure
    from celerp_docs import periodic

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    subs, books = await _companies(factory)
    day = date(2026, 7, 1)
    await periodic.tick(factory, today=day)
    ran = sorted((name, cid) for name, cid, *_ in ops)
    expected = [("a", subs)]
    if "celerp-subscriptions" in dependency_closure(["celerp-accounting"]):
        expected.append(("a", books))
    if "celerp-accounting" in dependency_closure(["celerp-subscriptions"]):
        expected.append(("b", subs))
    expected.append(("b", books))
    assert ran == sorted(expected)
    assert {(today, budget) for _, _, today, budget in ops} == {(day, 10)}
    assert await _written(factory, subs) == sorted(f"periodic-{n}" for n, c in expected if c == subs)


async def test_dispatcher_one_company_failure_isolated(committed_engine, ops):
    from celerp_docs import periodic

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    from test_fresh_authority_races_pg import _seed

    failing, healthy = (await _seed(factory))["company_id"], (await _seed(factory))["company_id"]
    _FAIL_FOR.add(failing)
    await periodic.tick(factory, today=date(2026, 7, 1))
    # The failing operation's write is rolled back; its other operation and the other company commit.
    assert await _written(factory, failing) == ["periodic-b"]
    assert await _written(factory, healthy) == ["periodic-a", "periodic-b"]
    _FAIL_FOR.clear()
    await periodic.tick(factory, today=date(2026, 7, 1))  # the next pass retries it
    assert await _written(factory, failing) == ["periodic-a", "periodic-b", "periodic-b"]


async def test_dispatcher_skips_company_staged_for_migration(committed_engine, ops, monkeypatch):
    from celerp_docs import periodic

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    from test_fresh_authority_races_pg import _seed

    staged, live = (await _seed(factory))["company_id"], (await _seed(factory))["company_id"]

    async def _staged(session, company_id):
        return company_id == staged

    monkeypatch.setattr(periodic, "is_company_migration_staged", _staged)
    await periodic.tick(factory, today=date(2026, 7, 1))
    async with factory() as s:
        total = (await s.execute(select(func.count()).select_from(Location).where(
            Location.company_id == staged, Location.name.like("periodic-%")))).scalar_one()
    assert total == 0
    assert await _written(factory, live) == ["periodic-a", "periodic-b"]


def test_check_dispatcher_does_not_import_fixed_assets():
    """The dispatcher and subscription billing import nothing from fixed assets, so
    they run for a company that does not use it."""
    paths = [str(_ROOT)] + [str(p) for p in sorted((_ROOT / "default_modules").iterdir()) if p.is_dir()]
    probe = (
        "import sys; sys.path[:0] = %r\n"
        "import celerp_docs.periodic, celerp_subscriptions.billing\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'celerp_fixed_assets')\n"
        "print(','.join(bad)); sys.exit(1 if bad else 0)\n"
    ) % paths
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.stdout, done.stderr[-2000:])


def test_periodic_operation_slot_first_party_only():
    """Only Celerp's own modules may add an operation the dispatcher runs unattended."""
    from celerp.modules import slots

    entry = {"handler": "acme.ops:run", "module": "acme"}
    with pytest.raises(ValueError, match="is filled by Celerp's own modules only"):
        slots.check("periodic_operation", entry)
    slots.check("periodic_operation", {**entry, "_first_party": True})
