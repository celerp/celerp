# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A backup taken before unknown goods costs were kept restores unchanged.

``fixtures/inv_unknown_cost`` was captured by driving the older release's own API
(``fixtures/inv_unknown_cost/capture.py``, run from a checkout of that release): a cleared goods
cost on a lot carrying freight, a merge of a known and an unknown source, a merge of a lot
carrying a bill's freight, a priced receipt into a lot, a split and a sold-out lot. Restored
today, every lot keeps the state the older release gave it, and rebuilding the company from its
events gives the same states."""
from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from celerp.accounting_roles import LOT_ACCOUNT_FIELD, SEEDED_TARGETS, R
from migration_support import code_config, maker, real_client, real_engine  # noqa: F401
from test_company_backup import _BK_UUID, _bk_local, _bk_restore_new, _bk_setup
from test_company_backup_snapshot import _replayed_states

pytestmark = pytest.mark.asyncio
FIXTURE = Path(__file__).parent / "fixtures" / "inv_unknown_cost"
OPENING = SEEDED_TARGETS[R.INVENTORY_OPENING]


def _normalized(states) -> list[str]:
    return sorted(_BK_UUID.sub("<id>", json.dumps(s, sort_keys=True, default=str)) for s in states)


async def test_backup_from_before_restores_and_replays_unchanged(real_engine, real_client, tmp_path, monkeypatch):
    from celerp.models.projections import Projection

    from packaging.version import Version
    from celerp.migrations import compatibility

    # This copy runs as the release after the capture: a checkout without tags (CI's)
    # reports a version below any release, and would refuse the copy as newer.
    archive = FIXTURE / "company.celerp-company"
    with zipfile.ZipFile(archive) as z:
        captured_by = Version(json.loads(z.read("manifest.json"))["celerp_version"])
    monkeypatch.setattr(compatibility, "running_version",
                        lambda: f"{captured_by.major}.{captured_by.minor}.{captured_by.micro + 1}")
    _bk_local(monkeypatch, tmp_path)
    _modules_running(monkeypatch)
    _user, _cid, tok = await _bk_setup(real_engine)
    cid = await _bk_restore_new(real_client, tok, archive.read_bytes())
    async with maker(real_engine)() as s:
        items = (await s.execute(select(Projection.state).where(
            Projection.company_id == cid, Projection.entity_type == "item"))).scalars().all()
    # The copy's accounts mean its company runs Accounting here, and a company coming in with
    # Accounting places the stock its older release left on no inventory account, as on the
    # opening stock every other lot of the copy already holds.
    captured = json.loads((FIXTURE / "items.json").read_text())["items"].values()
    assert {s.get(LOT_ACCOUNT_FIELD) for s in captured} == {OPENING, None}
    assert _normalized(items) == _normalized({LOT_ACCOUNT_FIELD: OPENING, **s} for s in captured)

    stored, replayed = await _replayed_states(real_engine, cid)
    diff = {k: (stored.get(k), replayed.get(k)) for k in stored.keys() | replayed.keys()
            if stored.get(k) != replayed.get(k)}
    assert not diff, diff



def _as_next_release(monkeypatch) -> bytes:
    """The fixture's bytes, read by a Celerp one minor release after the one that made it."""
    from packaging.version import Version
    from celerp.migrations import compatibility

    archive = FIXTURE / "company.celerp-company"
    with zipfile.ZipFile(archive) as z:
        captured_by = Version(json.loads(z.read("manifest.json"))["celerp_version"])
    monkeypatch.setattr(compatibility, "running_version", lambda: f"{captured_by.major}.{captured_by.minor + 1}.0")
    return archive.read_bytes()


def _modules_running(monkeypatch) -> None:
    """The modules holding the copy's data run here, as they do once the owner turned them on."""
    from celerp.modules import loader
    names = {"celerp-accounting", "celerp-inventory", "celerp-docs"}
    monkeypatch.setattr(loader, "_loaded", [*(m for m in loader._loaded if m["name"] not in names),
                                            *({"name": n, "version": "99.0.0"} for n in sorted(names))])


async def _fresh_installation(engine) -> None:
    from sqlalchemy import text
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))


async def test_older_copy_at_first_boot_asks_for_the_modules_its_data_needs(real_engine, real_client, tmp_path,
                                                                           monkeypatch, code_config):
    """A copy made before backups named their modules, read before any company exists (so the
    accounting tables are not in place yet), asks for the module holding its accounts instead
    of being refused as made by a newer version."""
    from test_company_backup import _bk_not_running, _r_bread, _r_env

    data = _as_next_release(monkeypatch)
    _r_env(tmp_path, monkeypatch)
    await _fresh_installation(real_engine)
    _bk_not_running(monkeypatch, "celerp-accounting")
    r = await _r_bread(real_client, data, code_config)
    assert r.status_code == 200, r.text
    needed = {m["name"]: m["status"] for m in r.json()["modules"]}
    assert r.json()["modules_ready"] is False and needed.get("celerp-accounting") == "enable_required", needed


async def test_older_copy_opens_as_the_first_company_with_the_modules_its_data_needs(real_engine, real_client,
                                                                                     tmp_path, monkeypatch,
                                                                                     code_config):
    """Opened as the first company, the copy's company runs the modules whose data it carries,
    its accounts and the items, documents and journal entries its ledger records; a module
    holding nothing in it stays off."""
    from celerp.models.company import Company
    from celerp.modules.registry import get_enabled
    from test_company_backup import _r_bootstrap, _r_created, _r_env

    data = _as_next_release(monkeypatch)
    _r_env(tmp_path, monkeypatch)
    _modules_running(monkeypatch)
    await _fresh_installation(real_engine)
    body = _r_created(await _r_bootstrap(real_client, data, code_config))
    async with maker(real_engine)() as s:
        enabled = get_enabled((await s.get(Company, uuid.UUID(body["company_id"]))).settings)
    assert {"celerp-accounting", "celerp-inventory", "celerp-docs"} <= enabled, enabled
    assert "celerp-labels" not in enabled and "celerp-manufacturing" not in enabled, enabled


def _labels_table_not_here(monkeypatch) -> None:
    """The Labels module never ran here, so its table is not in place, as on a fresh installation."""
    import dataclasses
    from celerp.services import company_backup as cb

    real = cb._classify

    async def without_labels(*args, **kwargs):
        plan = await real(*args, **kwargs)
        return dataclasses.replace(plan, order=[n for n in plan.order if n != "label_templates"],
                                   schema={n: v for n, v in plan.schema.items() if n != "label_templates"})
    monkeypatch.setattr(cb, "_classify", without_labels)


async def test_older_copy_opens_where_a_module_it_holds_nothing_for_never_ran(real_engine, real_client, tmp_path,
                                                                            monkeypatch, code_config):
    """The copy carries the Labels table empty. Where Labels never ran that table is not in place,
    and an empty table of a module carries nothing, so the copy opens instead of being refused as
    made by a newer version."""
    from test_company_backup import _r_bootstrap, _r_created, _r_env

    data = _as_next_release(monkeypatch)
    _r_env(tmp_path, monkeypatch)
    _modules_running(monkeypatch)
    _labels_table_not_here(monkeypatch)
    await _fresh_installation(real_engine)
    with zipfile.ZipFile(FIXTURE / "company.celerp-company") as z:
        assert json.loads(z.read("manifest.json"))["tables"]["label_templates"]["rows"] == 0
    _r_created(await _r_bootstrap(real_client, data, code_config))


async def test_a_table_this_installation_lacks_still_refuses_when_the_copy_holds_rows_in_it(
        real_engine, real_client, tmp_path, monkeypatch, code_config):
    """Rows in a table that is not in place here cannot be carried: still made by a newer version."""
    import dataclasses
    from celerp.services import company_backup as cb
    from test_company_backup import _r_bread, _r_env, _r_refused

    data = _as_next_release(monkeypatch)
    _r_env(tmp_path, monkeypatch)
    _modules_running(monkeypatch)
    real = cb._classify

    async def without_locations(*args, **kwargs):
        plan = await real(*args, **kwargs)
        return dataclasses.replace(plan, order=[n for n in plan.order if n != "locations"],
                                   schema={n: v for n, v in plan.schema.items() if n != "locations"})
    monkeypatch.setattr(cb, "_classify", without_locations)
    await _fresh_installation(real_engine)
    r = await _r_bread(real_client, data, code_config)
    _r_refused(r)
    assert "newer version" in r.json()["detail"], r.text
