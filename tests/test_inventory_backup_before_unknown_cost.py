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
    _accounting_running(monkeypatch)
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


def _accounting_running(monkeypatch) -> None:
    """The module holding the copy's accounts runs here, as it does once the owner turned it on."""
    from celerp.modules import loader
    monkeypatch.setattr(loader, "_loaded", [*(m for m in loader._loaded if m["name"] != "celerp-accounting"),
                                            {"name": "celerp-accounting", "version": "99.0.0"}])


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
    """Opened as the first company, the copy's company runs the modules whose data it carries;
    a module holding nothing in it stays off."""
    from celerp.models.company import Company
    from celerp.modules.registry import get_enabled
    from test_company_backup import _r_bootstrap, _r_created, _r_env

    data = _as_next_release(monkeypatch)
    _r_env(tmp_path, monkeypatch)
    _accounting_running(monkeypatch)
    await _fresh_installation(real_engine)
    body = _r_created(await _r_bootstrap(real_client, data, code_config))
    async with maker(real_engine)() as s:
        enabled = get_enabled((await s.get(Company, uuid.UUID(body["company_id"]))).settings)
    assert "celerp-accounting" in enabled and "celerp-labels" not in enabled, enabled
