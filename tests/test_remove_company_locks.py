"""The suite's company cleanup (test_helpers.remove_company) deletes each company's rows
under that company's lock, as Celerp's own removals do. A row taken without it counts, in
the lock checker's census, as a taker the company lock does not keep out, and leaves every
inversion gated by that lock unproven. Connector execution lease rows are taken holding
nothing else, the company lock included, by design (celerp.connectors.execution)."""
from __future__ import annotations

import sys
import uuid

from migration_support import maker, real_engine  # noqa: F401 - fixture
from test_helpers import remove_company


_LEASES = {"row:connector_execution_leases", "row:connector_execution_heartbeats"}


def _ungated(cls_info: dict) -> set[str]:
    return {s for c, v in cls_info.items() if c not in _LEASES for s in v.get("ungated_sites", ())}


async def test_removing_a_company_takes_none_of_its_rows_without_its_lock(real_engine):
    from celerp.models.company import Company, Location

    ids = [uuid.uuid4(), uuid.uuid4()]
    async with maker(real_engine)() as s:
        for n, company_id in enumerate(ids):
            s.add(Company(id=company_id, name=f"Shop {n}", slug=f"shop-{n}-{uuid.uuid4().hex[:6]}",
                          settings={}, is_active=True))
        await s.flush()
        for company_id in ids:
            s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse"))
        await s.commit()
    recorder = sys.modules["lock_rank"].RECORDER
    before = _ungated(recorder.dump()["classes"])
    await remove_company(maker(real_engine), ids)
    added = _ungated(recorder.dump()["classes"]) - before
    assert not {s for s in added if "remove_company" in s}, sorted(added)
