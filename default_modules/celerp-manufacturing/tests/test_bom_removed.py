# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Proof the standalone BOM entity is removed but the ledger stays replayable (no back-compat)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from celerp.models.company import Company, User
from celerp.models.projections import Projection
from celerp_manufacturing.migrations import migrate_boms_to_recipes
from celerp_manufacturing.projection_handler import apply_manufacturing_event


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@bomrm.test"
    r = await client.post("/auth/register", json={"company_name": "BomRm Co", "email": addr, "name": "Admin", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


# --- inert replay (pure) ----------------------------------------------------

def test_legacy_bom_event_replay_inert() -> None:
    state = {"sku": "X", "quantity": 5}
    for et in ("bom.created", "bom.updated", "bom.deleted"):
        assert apply_manufacturing_event(state, et, {"name": "old", "components": [{"sku": "Y"}]}) == state


def test_unknown_mfg_event_still_raises() -> None:
    with pytest.raises(ValueError):
        apply_manufacturing_event({}, "mfg.nope", {})


# --- endpoints gone ---------------------------------------------------------

@pytest.mark.asyncio
async def test_manufacturing_boms_endpoints_gone_404(client) -> None:
    token = await _register(client)
    # GET hits the catch-all /{order_id} → 404 (no such order); POST has no route → 405.
    # Either way the dedicated BOM endpoints are gone.
    assert (await client.get("/manufacturing/boms", headers=_h(token))).status_code in (404, 405)
    assert (await client.post("/manufacturing/boms", headers=_h(token), json={"name": "x"})).status_code in (404, 405)


# --- migration --------------------------------------------------------------

@pytest.mark.asyncio
async def test_bom_migration_converts_to_recipe(client, session) -> None:
    token = await _register(client)
    raw = (await client.post("/items", headers=_h(token), json={"sku": "RAW", "name": "Raw", "quantity": 10, "sell_by": "piece", "cost_total": 50})).json()["id"]
    fg = (await client.post("/items", headers=_h(token), json={"sku": "FG", "name": "FG", "sell_by": "piece"})).json()["id"]

    company = (await session.execute(select(Company))).scalars().first()
    user = (await session.execute(select(User))).scalars().first()

    # Seed a legacy BOM projection directly (the create path is gone).
    session.add(Projection(
        company_id=company.id, entity_id="bom:legacy-1", entity_type="bom", version=0,
        updated_at=datetime.now(timezone.utc),
        state={"name": "FG BOM", "output_item_id": fg, "output_qty": 1,
               "components": [{"item_id": raw, "sku": "RAW", "qty": 2, "unit": "piece"}]},
    ))
    await session.commit()

    n = await migrate_boms_to_recipes(session, company.id, user.id)
    await session.commit()
    assert n == 1

    item = (await client.get(f"/items/{fg}", headers=_h(token))).json()
    comps = item["recipe"]["components"]
    assert len(comps) == 1 and comps[0]["item_id"] == raw and comps[0]["quantity"] == 2


@pytest.mark.asyncio
async def test_bom_migration_noop_when_none(client, session) -> None:
    await _register(client)
    company = (await session.execute(select(Company))).scalars().first()
    user = (await session.execute(select(User))).scalars().first()
    assert await migrate_boms_to_recipes(session, company.id, user.id) == 0


# --- grep gate (cruft) ------------------------------------------------------

def test_no_bom_entity_references_in_live_code() -> None:
    """No standalone-BOM entity symbols remain in the manufacturing module / UI (migration excluded)."""
    root = Path(__file__).resolve().parents[3]
    targets = [
        root / "default_modules/celerp-manufacturing/celerp_manufacturing/routes.py",
        root / "default_modules/celerp-manufacturing/celerp_manufacturing/projection_handler.py",
        root / "ui/routes/manufacturing.py",
        root / "ui/api_client.py",
    ]
    forbidden = ["/manufacturing/boms", "BOMCreate", "BOMUpdate", "BOMComponent",
                 "_bom_list_table", "_bom_detail_section", "_new_bom_form",
                 "list_boms", "create_bom", "delete_bom", "update_bom"]
    offenders = []
    for f in targets:
        text = f.read_text()
        for pat in forbidden:
            if pat in text:
                offenders.append(f"{f.name}: {pat}")
    # projection_handler may reference the inert 'bom.' prefix — that's allowed.
    assert offenders == [], offenders
