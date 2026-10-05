# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Registration's starter records and the modules that own them.

Registration gives a new company starter items (Inventory) and its own contact (Contacts).
An older release wrote them while no module was enabled, so an install that registered and
never enabled a module holds records no loaded module can apply. Its first start on this
release turns those two modules on (and says so) and starts with every record current;
records of any other module that is not enabled still hold the start back.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, text

import pre366
from test_i18n_posting_refusals import _catalog, shown_in
from celerp.models.company import Company
from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio

_MODULES = Path(__file__).resolve().parent.parent / "default_modules"
_STARTERS = {"celerp-inventory", "celerp-contacts"}
_MARKER = "starter_modules_enabled"
_STARTUP_LOOPS = (
    "celerp.connectors.outbound_queue.outbound_queue_loop",
    "celerp.connectors.daily_scheduler.scheduler_loop_all",
    "celerp.services.reorder.reorder_alert_loop",
    "celerp.services.update.update_loop",
    "celerp.ai.cleanup.run_cleanup_loop",
    "celerp.services.session_tracker.run_jti_cleanup_loop",
)


def _module_slots(name: str) -> dict:
    from celerp.modules.loader import read_manifest

    return read_manifest(_MODULES / name).get("slots") or {}


def _without(monkeypatch, *modules: str) -> None:
    """The projection handlers and start hooks of ``modules`` not loaded."""
    from celerp.modules import slots

    for slot in ("projection_handler", "on_modules_ready"):
        monkeypatch.setitem(slots._slots, slot, [c for c in slots.get(slot) if c.get("_module") not in modules])


def _load(module_dir, enabled) -> list:
    """Loading a module registers its projection handlers and start hooks."""
    from celerp.modules import slots

    for name in sorted(enabled):
        for slot in ("projection_handler", "on_modules_ready"):
            contribs = _module_slots(name).get(slot) or []
            for c in contribs if isinstance(contribs, list) else [contribs]:
                slots.register(slot, {**c, "_module": name})
    return []


def _installed(monkeypatch, tmp_path, enabled: list[str]) -> Path:
    """An installation whose saved module list is ``enabled``, started with the bundled
    modules on its module path."""
    import celerp.main as main_mod

    cfg = tmp_path / "config.toml"
    cfg.write_text("[modules]\nenabled = [" + ", ".join(f'"{m}"' for m in enabled) + "]\n")
    monkeypatch.setenv("CELERP_CONFIG", str(cfg))
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    monkeypatch.setenv("MODULE_DIR", str(_MODULES))
    monkeypatch.setattr(main_mod, "_MODULE_DIR", str(_MODULES))
    return cfg


async def _start(monkeypatch) -> None:
    """A normal start of this release, loading the modules the installation has enabled."""
    import celerp.main as main_mod
    from celerp.config import settings

    async def _migrated(_engine, enabled):
        return set(enabled), {}

    monkeypatch.setattr("celerp.modules.migrations_runner.run_migration_phase", _migrated)
    monkeypatch.setattr("celerp.modules.loader.load_all", _load)
    monkeypatch.setattr("celerp.modules.loader.register_api_routes", lambda *a, **k: None)
    monkeypatch.setattr("celerp.modules.loader.demoted_first_party", lambda *a, **k: [])
    monkeypatch.setattr("celerp.gateway.bootstrap.associate_partner_deployment", AsyncMock())
    monkeypatch.setattr("celerp.connectors.outbound_queue.adopt_legacy_connector_configs", AsyncMock())
    monkeypatch.setattr(main_mod, "_try_auto_activate", AsyncMock())
    monkeypatch.setattr("celerp.gateway.shutdown", AsyncMock())
    for target in _STARTUP_LOOPS:
        monkeypatch.setattr(target, lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(settings, "gateway_token", "")
    monkeypatch.setattr(settings, "celerp_public_url", None)
    conn = AsyncMock()
    begin = MagicMock()
    begin.__aenter__ = AsyncMock(return_value=conn)
    begin.__aexit__ = AsyncMock(return_value=False)
    with patch("celerp.main.lifecycle_engine", MagicMock(begin=MagicMock(return_value=begin))):
        async with main_mod.lifespan(main_mod.app):
            pass


async def _registered_on_older_release(session, *, with_run: bool = False) -> dict:
    """A company registered on an older release with no module enabled: its starter items
    and own contact stored as that release wrote them, and the start recorded as that
    release's."""
    from celerp.services import provisioning
    from test_helpers import make_authed_token

    user = await provisioning.create_install_owner(
        session, name="Owner", email=f"owner-{uuid.uuid4().hex[:8]}@test.co", password="x" * 12)
    company = await provisioning._create_company(session, owner=user, company_name="Starter Co", settings={})
    await provisioning._fire_company_created(session, company.id)
    cid = company.id
    contact = f"contact:{uuid.uuid4()}"
    seeds = [
        ("item:demo-1", "item", "item.created", {"sku": "DEMO-1", "name": "Sample", "quantity": 1},
         "demo", f"demo:item:{cid}:DEMO-1"),
        (contact, "contact", "crm.contact.created",
         {"name": "Owner", "company_name": "Starter Co", "contact_type": "both", "is_self": True},
         "registration", f"reg:contact:self:{cid}"),
    ]
    if with_run:
        seeds.append(("mfg:1", "mfg_order", "mfg.order.created",
                      {"product_sku": "FG", "quantity": 1, "inputs": [], "outputs": []}, "api", "mfg-1"))
    now = datetime.now(timezone.utc)
    for entity_id, entity_type, event_type, data, source, key in seeds:
        session.add(LedgerEntry(company_id=cid, entity_id=entity_id, entity_type=entity_type,
                                event_type=event_type, data=data, actor_id=user.id, location_id=None,
                                source=source, idempotency_key=key, metadata_={}))
        session.add(Projection(company_id=cid, entity_id=entity_id, entity_type=entity_type,
                               state=data, version=1, location_id=None, created_at=now, updated_at=now))
        await session.flush()
    company.settings = {"self_contact_id": contact}
    await session.commit()
    await pre366.last_started_on_older_release(session)
    conn = await session.connection()
    await conn.execute(text("DELETE FROM instance_meta WHERE key = :k"), {"k": _MARKER})
    await session.commit()
    token = await make_authed_token(session, str(user.id), str(cid), "owner")
    return {"company_id": cid, "headers": {"Authorization": f"Bearer {token}"}}


def _enabled(cfg: Path) -> set[str]:
    import tomllib

    return set(tomllib.loads(cfg.read_text()).get("modules", {}).get("enabled") or [])


async def _company_settings(session, company_id) -> dict:
    session.expire_all()
    return (await session.get(Company, company_id)).settings or {}


async def test_an_install_that_registered_and_never_enabled_a_module_starts_current(
        client, session, monkeypatch, tmp_path):
    """The first start of this release on an install that registered on an older release and
    never enabled a module: the modules that own registration's starter records are turned on
    and loaded in that start, which brings every record current, accepts changes, and tells
    the company which modules were turned on and why. That happens once: a starter module
    the owner turns off later stays off at the next start."""
    from celerp.main import app
    from celerp.models.notification import Notification

    _without(monkeypatch, *_STARTERS)
    cfg = _installed(monkeypatch, tmp_path, [])
    old = await _registered_on_older_release(session)

    await _start(monkeypatch)

    assert app.state.data_current is True
    assert _STARTERS <= _enabled(cfg)
    # The company keeps using every enabled module; it is never narrowed to these two.
    assert "enabled_modules" not in await _company_settings(session, old["company_id"])
    item = await client.post("/items", json={"sku": "NEW-1", "name": "New", "sell_by": "piece"}, headers=old["headers"])
    assert item.status_code == 200, item.text
    contact = await client.post("/crm/contacts", json={"name": "Buyer"}, headers=old["headers"])
    assert contact.status_code == 200, contact.text
    [notice] = (await session.execute(select(Notification).where(
        Notification.company_id == old["company_id"], Notification.title == "Inventory and Contacts were turned on",
    ))).scalars().all()
    assert "starter" in notice.body and "Modules" in notice.body, notice.body
    key = "notice.starter_modules_on"
    assert notice.i18n == {"title": f"{key}.title", "body": f"{key}.body"}
    de = _catalog("de")
    assert shown_in("de", notice) == {"id": str(notice.id), "title": de[f"{key}.title"], "body": de[f"{key}.body"]}

    cfg.write_text('[modules]\nenabled = ["celerp-inventory"]\n')
    await _start(monkeypatch)
    assert _enabled(cfg) == {"celerp-inventory"}


async def test_starter_records_never_turn_on_a_module_that_holds_other_records(
        client, session, monkeypatch, tmp_path):
    """Records of a module that is not enabled (a manufacturing run) still hold the start
    back once the starter modules are on; only the starter modules are turned on."""
    from celerp.main import app

    _without(monkeypatch, *_STARTERS, "celerp-manufacturing")
    cfg = _installed(monkeypatch, tmp_path, [])
    old = await _registered_on_older_release(session, with_run=True)

    await _start(monkeypatch)

    assert app.state.data_current is False
    assert _enabled(cfg) & {"celerp-manufacturing"} == set()
    assert _STARTERS <= _enabled(cfg)
    r = await client.post("/items", json={"sku": "NEW-1", "name": "New", "sell_by": "piece"}, headers=old["headers"])
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert detail["message_key"] == "held_back.refused.modules_off.one", detail
    assert detail["params"]["modules"] == "Manufacturing", detail


async def test_registration_writes_no_record_a_loaded_module_cannot_apply(session, monkeypatch):
    """Registration on a start that loaded neither Inventory nor Contacts writes no starter
    item or own contact (nothing could apply them); the company is still created."""
    from celerp.services import provisioning

    _without(monkeypatch, *_STARTERS)

    company, _user = await provisioning.provision_registered_company(
        session, company_name="Fresh Co", owner_name="Owner",
        email=f"fresh-{uuid.uuid4().hex[:8]}@test.co", password="x" * 12)

    types = set((await session.execute(select(LedgerEntry.event_type).where(
        LedgerEntry.company_id == company.id))).scalars())
    assert not {t for t in types if t.startswith(("item.", "crm.contact."))}, types


async def test_registration_seeds_the_starter_records_without_narrowing_the_company(session):
    """Registration with Inventory and Contacts loaded seeds the starter records, and the new
    company lists no modules of its own, so it uses every module the install enables."""
    from celerp.services import provisioning

    company, _user = await provisioning.provision_registered_company(
        session, company_name="Fresh Co", owner_name="Owner",
        email=f"fresh-{uuid.uuid4().hex[:8]}@test.co", password="x" * 12)

    types = set((await session.execute(select(LedgerEntry.event_type).where(
        LedgerEntry.company_id == company.id))).scalars())
    assert {"item.created", "crm.contact.created"} <= types
    assert "enabled_modules" not in await _company_settings(session, company.id)
