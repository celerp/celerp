# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A slot core calls on every item write or upgrade is checked when it is filled: a
contribution without a handler is refused, and only Celerp's own modules may tell the
upgrade what older production runs hold."""
from __future__ import annotations

import sys
import textwrap
import uuid

import pytest

from celerp.models.accounting import UserCompany
from celerp.models.company import Company, User
from celerp.modules import loader, slots
from test_helpers import make_authed_token


@pytest.fixture
def clean_slots():
    saved = slots.all_slots()
    loaded, errors = list(loader._loaded), dict(loader._load_errors)
    yield
    slots._slots.clear()
    slots._slots.update(saved)
    loader._loaded[:] = loaded
    loader._load_errors.clear()
    loader._load_errors.update(errors)
    for key in [k for k in sys.modules if k.startswith("acme_slot_")]:
        sys.modules.pop(key, None)


def _module(base, name: str, slot: str, contribution: str):
    pkg = base / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(textwrap.dedent(f"""
        async def in_production(*, session, company_id):
            return 1000

        PLUGIN_MANIFEST = {{"name": "{name}", "version": "1.0", "slots": {{"{slot}": {contribution}}}}}
    """).strip())


@pytest.mark.parametrize("slot", ["item_lineage_guard", "inventory_in_production"])
def test_a_contribution_without_a_handler_is_refused_when_registered(clean_slots, slot):
    with pytest.raises(ValueError, match="handler"):
        slots.register(slot, {"_module": "acme-thirdparty", "_first_party": True})
    assert slots.get(slot) == [c for c in slots.get(slot) if c.get("handler")]


@pytest.mark.asyncio
async def test_item_writes_keep_working_after_a_handlerless_guard_is_offered(client, session, clean_slots):
    with pytest.raises(ValueError):
        slots.register("item_lineage_guard", {"_module": "acme-thirdparty"})
    cid, uid = uuid.uuid4(), uuid.uuid4()
    session.add(Company(id=cid, name="Slot Co", slug=f"slot-{cid.hex[:8]}", settings={}))
    session.add(User(id=uid, email=f"slot-{cid.hex[:8]}@test.co", name="Admin", auth_hash="x", is_active=True))
    await session.flush()
    session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=cid, role="admin", is_active=True))
    await session.commit()
    token = await make_authed_token(session, str(uid), str(cid), "admin")
    r = await client.post("/items", json={"sku": "S-1", "name": "S", "sell_by": "piece"},
                          headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


def test_a_third_party_module_cannot_tell_the_upgrade_what_production_holds(clean_slots):
    with pytest.raises(ValueError, match="own modules"):
        slots.register("inventory_in_production",
                       {"handler": "acme_slot_probe:in_production", "_module": "acme-thirdparty"})
    assert all(c.get("_first_party") is True for c in slots.get("inventory_in_production"))


def test_the_loader_refuses_a_third_party_module_filling_the_production_slot(clean_slots, tmp_path):
    _module(tmp_path, "acme_slot_tp", "inventory_in_production", '{"handler": "acme_slot_tp:in_production"}')
    before = slots.get("inventory_in_production")
    loader.load_all(tmp_path, {"acme_slot_tp"})
    assert "acme_slot_tp" in loader.load_errors()
    assert "own modules" in loader.load_errors()["acme_slot_tp"]
    assert slots.get("inventory_in_production") == before


def test_the_loader_refuses_a_guard_without_a_handler_and_registers_nothing_of_the_module(clean_slots, tmp_path):
    pkg = tmp_path / "acme_slot_nh"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        'PLUGIN_MANIFEST = {"name": "acme_slot_nh", "version": "1.0", "slots": {'
        '"nav": {"label": "Acme", "href": "/acme", "order": 90}, "item_lineage_guard": {"note": "x"}}}')
    loader.load_all(tmp_path, {"acme_slot_nh"})
    assert "handler" in loader.load_errors()["acme_slot_nh"]
    assert not [c for c in slots.get("nav") if c.get("_module") == "acme_slot_nh"]
