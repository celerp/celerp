# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Test support for the fixed assets module: a started demo company and the calls tests make."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

API = "/fixed-assets/api"


class FA:
    """A started demo company and the calls its tests make through the real routes."""

    def __init__(self, client, session, auth):
        self.client, self.session, self.auth = client, session, auth
        self.h = auth["headers"]

    async def get(self, path: str, **params):
        return await self.client.get(f"{API}{path}", headers=self.h, params=params)

    async def post(self, path: str, body: dict | None = None, headers=None):
        return await self.client.post(f"{API}{path}", headers=headers or self.h, json=body or {})

    async def patch(self, path: str, body: dict, headers=None):
        return await self.client.patch(f"{API}{path}", headers=headers or self.h, json=body)

    async def ok(self, response, status: int = 200) -> dict:
        assert response.status_code == status, response.text
        return response.json()

    async def intake(self) -> list[dict]:
        return (await self.ok(await self.get("/intake")))["items"]

    async def line(self, description: str) -> dict:
        found = [i for i in await self.intake() if description in i["description"]]
        assert len(found) == 1, (description, await self.intake())
        return found[0]

    async def classes(self) -> dict[str, dict]:
        return {c["name"]: c for c in (await self.ok(await self.get("/classes")))["items"]}

    async def operate(self, path: str, body: dict, *, key: str, headers=None) -> dict:
        """Preview, then confirm what the preview showed."""
        preview = await self.ok(await self.post(path, {**body, "preview": True, "op_key": key}, headers))
        return await self.ok(await self.post(
            path, {**body, "preview": False, "op_key": key, "fingerprint": preview["fingerprint"]}, headers))

    async def register(self, source: str, *, key: str = "reg-1", class_name: str = "Vehicles", **body) -> dict:
        cls = (await self.classes())[class_name]
        return await self.operate("/register", {"source": source, "class_id": cls["id"], **body}, key=key)

    async def asset(self, asset_id: str) -> dict:
        return await self.ok(await self.get(f"/assets/{asset_id}"))

    async def state(self, entity_id: str) -> dict | None:
        from celerp.models.projections import Projection

        self.session.expire_all()
        row = await self.session.get(Projection, {"company_id": self.auth["company_id"], "entity_id": entity_id})
        return row.state if row is not None else None

    async def entities(self, entity_type: str) -> list[dict]:
        from celerp.models.projections import Projection

        self.session.expire_all()
        rows = (await self.session.execute(select(Projection).where(
            Projection.company_id == self.auth["company_id"], Projection.entity_type == entity_type))).scalars()
        return [{"entity_id": r.entity_id, **r.state} for r in rows]

    async def balance(self, code: str) -> Decimal:
        """Debit minus credit on ``code`` across posted journals."""
        total = Decimal(0)
        for je in await self.entities("journal_entry"):
            if je.get("status") != "posted":
                continue
            for e in je.get("entries") or []:
                if e.get("account") == code:
                    total += Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
        return total

    async def module_journals(self) -> list[dict]:
        return [je for je in await self.entities("journal_entry")
                if je["entity_id"].startswith("je:mod:celerp-fixed-assets:")]

    async def journal(self, ts: str, entries: list[dict]) -> str:
        """Post a manual journal; returns its id."""
        r = await self.client.post("/accounting/journal-entries", headers=self.h, json={
            "ts": ts, "memo": "Manual", "idempotency_token": str(uuid.uuid4()), "entries": entries})
        assert r.status_code == 200, r.text
        return r.json()["je_id"]

    async def role_headers(self, role: str) -> dict:
        """Headers of a new member of this company holding ``role``."""
        return (await self.member(role))[1]

    async def member(self, role: str) -> tuple[uuid.UUID, dict]:
        """A new member of this company holding ``role``: their user id and headers."""
        from celerp.models.accounting import UserCompany
        from celerp.models.company import User
        from test_helpers import make_authed_token

        uid = uuid.uuid4()
        self.session.add(User(id=uid, email=f"{role}-{uid.hex[:8]}@test.co", name=role.title(), auth_hash="x",
                              is_active=True))
        await self.session.flush()
        self.session.add(UserCompany(id=uuid.uuid4(), user_id=uid, company_id=self.auth["company_id"], role=role,
                                     is_active=True))
        await self.session.commit()
        token = await make_authed_token(self.session, str(uid), str(self.auth["company_id"]), role)
        return uid, {"Authorization": f"Bearer {token}"}

    async def fold_matches(self, asset_id: str) -> None:
        """The asset's financial state equals the fold of its movements."""
        from celerp_fixed_assets.projections import fold_movements

        state = await self.state(asset_id)
        movements = [m for m in await self.entities("fa_movement") if m["asset_id"] == asset_id]
        folded = fold_movements(movements)
        for key in ("cost", "accumulated_depreciation", "accumulated_impairment", "revaluation_surplus", "quantity"):
            assert Decimal(str(state[key])) == Decimal(str(folded[key])), (key, state, folded)


async def demo_owner(client) -> dict:
    """A company signed up the way a user signs up, with its owner's headers."""
    r = await client.post("/auth/register", json={
        "company_name": "Demo Co", "email": f"fa-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = (await client.get("/companies/me", headers=headers)).json()
    return {"headers": headers, "company_id": uuid.UUID(me["id"])}


async def start_demo(client, session) -> FA:
    """A demo company carrying its asset bills (the reseed seeds them), started."""
    fa = FA(client, session, await demo_owner(client))
    r = await client.post("/companies/me/demo/reseed", headers=fa.h)
    assert r.status_code == 200, r.text
    await fa.ok(await fa.post("/start"))
    return fa
