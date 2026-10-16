# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Test support for the fixed assets module: a started demo company and the calls tests make."""
from __future__ import annotations

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


async def start_demo(client, session, auth) -> FA:
    fa = FA(client, session, auth)
    r = await client.post("/companies/me/demo/reseed", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await fa.ok(await fa.post("/start"))
    return fa
