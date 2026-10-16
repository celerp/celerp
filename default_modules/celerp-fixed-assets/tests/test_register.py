# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Assets, movements and source claims: registering what the books already hold.

Every scenario starts from a demo company carrying asset bills (a delivery van, three
laptops, an office fit-out under construction), started with the default settings.
A source line is capitalized once, the register and the ledger agree after every
operation, and the asset's financial state is always the fold of its movements.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fa_fixtures import FA

pytestmark = pytest.mark.asyncio

D = Decimal


def _nbv_holds(asset: dict) -> None:
    assert D(asset["nbv"]) == D(asset["cost"]) - D(asset["accumulated_depreciation"]) - D(
        asset["accumulated_impairment"]), asset


# ── Setup ────────────────────────────────────────────────────────────────────


async def test_start_creates_starter_classes_and_default_settings(fa):
    setup = await fa.ok(await fa.get("/setup"))
    assert setup["started"] is True
    s = setup["settings"]
    assert s["run_mode"] == "prepare" and s["convention"] == "full_month"
    first = date.fromisoformat(s["first_managed_period"])
    assert first.day == 1 and s["cutover"] == (first - timedelta(days=1)).isoformat()
    classes = await fa.classes()
    assert list(classes) == ["Land", "Buildings", "Plant and machinery", "Vehicles", "Furniture and fittings",
                             "Computer equipment", "Software", "Other intangibles", "Assets under construction"]
    assert classes["Vehicles"]["asset_account"] == "1210"
    assert classes["Vehicles"]["accumulated_depreciation_account"] == "1220"
    assert classes["Vehicles"]["depreciation_account"] == "6400"
    assert classes["Assets under construction"]["asset_account"] == "1240"
    assert classes["Land"]["method"] == "none"
    assert classes["Software"]["kind"] == "intangible"
    # Start again changes nothing.
    await fa.ok(await fa.post("/start"))
    assert list(await fa.classes()) == list(classes)


async def test_demo_reseed_seeds_asset_bills_once(fa):
    lines = await fa.intake()
    assert sorted(i["description"] for i in lines) == ["Delivery van", "Laptop", "Office fit-out"]
    van = await fa.line("Delivery van")
    assert D(van["amount"]) == D("32000") and van["account"] == "1210" and van["kind"] == "acquisition"
    assert (await fa.line("Laptop"))["quantity"] == 3
    assert (await fa.line("Office fit-out"))["account"] == "1240"
    r = await fa.client.post("/companies/me/demo/reseed", headers=fa.h)
    assert r.status_code == 200, r.text
    assert len(await fa.intake()) == 3
    assert await fa.balance("1210") == D("36350") and await fa.balance("1240") == D("18500")


async def test_class_create_and_archive(fa):
    created = await fa.ok(await fa.post("/classes", {
        "name": "Tools", "kind": "tangible", "method": "straight_line", "life_months": 36, "residual_percent": "0"}))
    assert created["asset_account"] == "1210"
    archived = await fa.ok(await fa.patch(f"/classes/{created['id']}", {"archived": True}))
    assert archived["archived"] is True
    assert "Tools" not in await fa.classes()
    r = await fa.post("/classes", {"name": "", "kind": "tangible"})
    assert r.status_code == 422


async def test_viewer_cannot_register_or_start(fa):
    viewer = await fa.role_headers("viewer")
    van = await fa.line("Delivery van")
    cls = (await fa.classes())["Vehicles"]
    r = await fa.post("/register", {"source": van["source"], "class_id": cls["id"], "mode": "separate",
                                    "preview": True, "op_key": "v-1"}, headers=viewer)
    assert r.status_code == 403
    assert (await fa.post("/start", headers=viewer)).status_code == 403
    assert (await fa.post("/assets", {"kind": "physical", "name": "Ladder", "op_key": "p"},
                          headers=viewer)).status_code == 403


# ── Registration from bills ──────────────────────────────────────────────────


async def test_register_van_posts_nothing_and_claims_the_line(fa):
    van = await fa.line("Delivery van")
    preview = await fa.ok(await fa.post("/register", {
        "source": van["source"], "class_id": (await fa.classes())["Vehicles"]["id"], "mode": "separate",
        "preview": True, "op_key": "reg-van"}))
    assert preview["journals"] == []
    assert [a["cost"] for a in preview["assets"]] == ["32000.00"]
    done = await fa.register(van["source"], key="reg-van", mode="separate")
    (asset,) = done["assets"]
    assert asset["number"] == "FA-0001"
    got = await fa.asset(asset["id"])
    assert D(got["cost"]) == D("32000") and got["status"] == "active" and got["name"] == "Delivery van"
    assert got["available_date"] == van["date"]
    assert [s["source"] for s in got["sources"]] == [van["source"]]
    assert await fa.module_journals() == []
    assert all(i["source"] != van["source"] for i in await fa.intake())
    _nbv_holds(got)
    await fa.fold_matches(asset["id"])


async def test_register_separate_splits_cost_pro_rata(fa):
    laptops = await fa.line("Laptop")
    done = await fa.register(laptops["source"], key="reg-lap", class_name="Computer equipment", mode="separate")
    assert [a["number"] for a in done["assets"]] == ["FA-0001", "FA-0002", "FA-0003"]
    costs = [D((await fa.asset(a["id"]))["cost"]) for a in done["assets"]]
    assert costs == [D("1450")] * 3 and sum(costs) == D(laptops["amount"])
    for a in done["assets"]:
        assert (await fa.asset(a["id"]))["quantity"] == 1


async def test_register_quantity_record(fa):
    laptops = await fa.line("Laptop")
    done = await fa.register(laptops["source"], key="reg-q", class_name="Computer equipment", mode="quantity")
    (asset,) = done["assets"]
    got = await fa.asset(asset["id"])
    assert got["quantity"] == 3 and D(got["cost"]) == D("4350")
    await fa.fold_matches(asset["id"])


async def test_register_into_other_account_class_posts_reclass(fa):
    van = await fa.line("Delivery van")
    preview = await fa.ok(await fa.post("/register", {
        "source": van["source"], "class_id": (await fa.classes())["Assets under construction"]["id"],
        "mode": "separate", "preview": True, "op_key": "reg-auc"}))
    (je,) = preview["journals"]
    assert [(l["account"], l["debit"], l["credit"]) for l in je["lines"]] == [
        ("1240", "32000.00", "0.00"), ("1210", "0.00", "32000.00")]
    done = await fa.register(van["source"], key="reg-auc", class_name="Assets under construction", mode="separate")
    assert len(done["journals"]) == 1
    assert await fa.balance("1210") == D("4350") and await fa.balance("1240") == D("50500")
    (je_state,) = await fa.module_journals()
    assert je_state["status"] == "posted"


async def test_expense_instead_below_threshold(fa):
    await fa.ok(await fa.patch("/settings", {"threshold": "2000"}))
    laptops = await fa.line("Laptop")
    assert laptops["below_threshold"] is True
    assert (await fa.line("Delivery van"))["below_threshold"] is False
    done = await fa.operate("/register", {"source": laptops["source"], "mode": "expense", "expense_account": "6800"},
                            key="exp-1")
    assert done["assets"] == []
    assert await fa.balance("6800") == D("4350") and await fa.balance("1210") == D("32000")
    assert all(i["source"] != laptops["source"] for i in await fa.intake())


async def test_s1_retry_same_key_same_result_new_key_refused(fa):
    van = await fa.line("Delivery van")
    cls = (await fa.classes())["Vehicles"]
    body = {"source": van["source"], "class_id": cls["id"], "mode": "separate"}
    preview = await fa.ok(await fa.post("/register", {**body, "preview": True, "op_key": "k1"}))
    confirm = {**body, "preview": False, "op_key": "k1", "fingerprint": preview["fingerprint"]}
    first = await fa.ok(await fa.post("/register", confirm))
    again = await fa.ok(await fa.post("/register", confirm))
    assert again["assets"] == first["assets"]
    assert len(await fa.entities("fa_asset")) == 1
    r = await fa.post("/register", {**confirm, "op_key": "k2"})
    assert r.status_code == 409 and "FA-0001" in r.json()["detail"]
    changed = await fa.post("/register", {**confirm, "mode": "quantity"})
    assert changed.status_code == 409


async def test_s1_stale_form_after_claim_and_release_refused(fa):
    van = await fa.line("Delivery van")
    cls = (await fa.classes())["Vehicles"]
    body = {"source": van["source"], "class_id": cls["id"], "mode": "separate"}
    form_a = await fa.ok(await fa.post("/register", {**body, "preview": True, "op_key": "a"}))
    done = await fa.register(van["source"], key="b", mode="separate")
    await fa.operate(f"/assets/{done['assets'][0]['id']}/undo", {}, key="b-undo")
    assert (await fa.line("Delivery van"))["generation"] == 1
    r = await fa.post("/register", {**body, "preview": False, "op_key": "a", "fingerprint": form_a["fingerprint"]})
    assert r.status_code == 409
    assert "registered and released since you opened this form" in r.json()["detail"]
    redo = await fa.operate("/register", body, key="a2")
    assert len(redo["assets"]) == 1


async def test_s1_concurrent_registration_one_claim(committed_engine):
    from celerp.models.projections import Projection
    from celerp_fixed_assets import operations, setup
    from celerp_fixed_assets.demo import seed_demo_assets
    from celerp_fixed_assets.sources import intake
    from test_helpers import company_auth
    import sqlalchemy as sa

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, uid = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        await company_auth(s, cid, uid)
        await seed_demo_assets(s, cid, uid)
        await setup.start(s, cid, uid)
        await s.commit()
    async with factory() as s:
        van = next(i for i in await intake(s, cid) if i["description"] == "Delivery van")
        cls = next(c for c in await setup.classes(s, cid) if c["name"] == "Vehicles")

    async def register(key: str):
        body = {"source": van["source"], "class_id": cls["id"], "mode": "separate", "op_key": key}
        async with factory() as s:
            preview = await operations.register(s, cid, uid, {**body, "preview": True})
            await s.rollback()
        async with factory() as s:
            try:
                result = await operations.register(s, cid, uid, {**body, "preview": False,
                                                                 "fingerprint": preview["fingerprint"]})
                await s.commit()
                return result
            except HTTPException as exc:
                await s.rollback()
                return exc

    results = await asyncio.gather(register("tab-1"), register("tab-2"))
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, HTTPException)]
    assert len(wins) == 1 and len(losses) == 1
    assert losses[0].status_code == 409 and "FA-0001" in str(losses[0].detail)
    async with factory() as s:
        assets = (await s.execute(sa.select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "fa_asset"))).scalars().all()
        claims = (await s.execute(sa.select(Projection).where(
            Projection.company_id == cid, Projection.entity_type == "fa_src"))).scalars().all()
    assert len(assets) == 1
    assert [c.state["status"] for c in claims] == ["claimed"]


async def test_undo_registration_releases_line_and_reverses_reclass(fa):
    van = await fa.line("Delivery van")
    done = await fa.register(van["source"], key="reg", class_name="Assets under construction", mode="separate")
    asset_id = done["assets"][0]["id"]
    preview = await fa.ok(await fa.post(f"/assets/{asset_id}/undo", {"preview": True, "op_key": "u"}))
    assert [a["number"] for a in preview["assets"]] == ["FA-0001"]
    await fa.operate(f"/assets/{asset_id}/undo", {}, key="u")
    assert (await fa.asset(asset_id))["status"] == "undone"
    line = await fa.line("Delivery van")
    assert line["generation"] == 1
    assert await fa.balance("1210") == D("36350") and await fa.balance("1240") == D("18500")
    (je,) = await fa.module_journals()
    assert je["status"] == "void"
    listed = await fa.ok(await fa.get("/assets"))
    assert listed["items"] == []


async def test_s1_sourced_cost_cannot_diverge(fa):
    van = await fa.line("Delivery van")
    asset = (await fa.register(van["source"], key="reg", mode="separate"))["assets"][0]
    got = await fa.asset(asset["id"])
    r = await fa.patch(f"/assets/{asset['id']}", {"changes": {"cost": "30000"}, "previous": {"cost": got["cost"]}})
    assert r.status_code == 422 and "Replace source" in r.json()["detail"]
    fields = await fa.ok(await fa.get(f"/assets/{asset['id']}/correct-fields"))
    assert "cost" not in {f["field"] for f in fields["fields"]}
    assert {"replace_source", "link_source"} <= {a["action"] for a in fields["source_actions"]}
    typed = await fa.post(f"/assets/{asset['id']}/correct", {"changes": {"cost": "30000"}, "preview": True,
                                                            "op_key": "c"})
    assert typed.status_code == 422 and "Replace source" in typed.json()["detail"]
    assert D((await fa.asset(asset["id"]))["cost"]) == D("32000")


async def test_descriptive_edit_compare_and_set(fa):
    van = await fa.line("Delivery van")
    asset = (await fa.register(van["source"], key="reg", mode="separate"))["assets"][0]
    ok = await fa.ok(await fa.patch(f"/assets/{asset['id']}", {
        "changes": {"name": "Van 1", "serial": "VIN123"}, "previous": {"name": "Delivery van", "serial": None}}))
    assert ok["name"] == "Van 1" and ok["serial"] == "VIN123"
    stale = await fa.patch(f"/assets/{asset['id']}", {"changes": {"name": "Van 2"}, "previous": {"name": "Delivery van"}})
    assert stale.status_code == 409 and "Changed by someone else" in stale.json()["detail"]
    assert (await fa.asset(asset["id"]))["name"] == "Van 1"


# ── Registration from manual journals ────────────────────────────────────────


async def test_manual_journal_new_acquisition(fa):
    first = (await fa.ok(await fa.get("/setup")))["settings"]["first_managed_period"]
    je_id = await fa.journal(first, [{"account": "1210", "debit": 5000}, {"account": "3100", "credit": 5000}])
    line = await fa.line("Manual")
    assert line["source"] == f"{je_id}:0" and D(line["amount"]) == D("5000")
    done = await fa.register(line["source"], key="mj", class_name="Plant and machinery", mode="separate",
                             name="Lathe")
    got = await fa.asset(done["assets"][0]["id"])
    assert got["name"] == "Lathe" and D(got["cost"]) == D("5000")
    # The credit side of the journal is never offered.
    assert all(not i["source"].startswith(je_id) for i in await fa.intake())


async def test_bill_journals_and_module_journals_are_never_offered_as_manual_lines(fa):
    sources = [i["source"] for i in await fa.intake()]
    assert not any(s.startswith("je:") for s in sources)


async def test_not_an_acquisition_requires_a_reason(fa):
    first = (await fa.ok(await fa.get("/setup")))["settings"]["first_managed_period"]
    je_id = await fa.journal(first, [{"account": "1210", "debit": 700}, {"account": "1111", "credit": 700}])
    source = f"{je_id}:0"
    r = await fa.post("/not-acquisition", {"source": source, "reason": "  ", "preview": True, "op_key": "na"})
    assert r.status_code == 422 and "reason" in r.json()["detail"].lower()
    await fa.operate("/not-acquisition", {"source": source, "reason": "Reclass of a prepayment"}, key="na")
    assert all(i["source"] != source for i in await fa.intake())
    claim = await fa.state(f"fa:src:{source}")
    assert claim["status"] == "claimed" and claim["allocation"] == "not_acquisition"
    assert claim["reason"] == "Reclass of a prepayment"


async def test_s1_link_existing_depreciation(fa):
    van = await fa.line("Delivery van")
    laptops = await fa.line("Laptop")
    a = (await fa.register(van["source"], key="r1", mode="separate"))["assets"][0]["id"]
    b = (await fa.register(laptops["source"], key="r2", class_name="Computer equipment", mode="quantity"))["assets"][0]["id"]
    first = (await fa.ok(await fa.get("/setup")))["settings"]["first_managed_period"]
    je_id = await fa.journal(first, [{"account": "6400", "debit": 900}, {"account": "1220", "credit": 900}])
    line = await fa.line("Manual")
    assert line["kind"] == "depreciation" and line["source"] == f"{je_id}:1" and D(line["amount"]) == D("900")
    before = await fa.balance("1220")
    wrong = await fa.post("/link-depreciation", {"source": line["source"], "preview": True, "op_key": "ld",
                                                 "allocations": [{"asset_id": a, "amount": "500"}]})
    assert wrong.status_code == 422
    done = await fa.operate("/link-depreciation", {"source": line["source"], "allocations": [
        {"asset_id": a, "amount": "600"}, {"asset_id": b, "amount": "300"}]}, key="ld")
    assert done["journals"] == []
    assert D((await fa.asset(a))["accumulated_depreciation"]) == D("600")
    assert D((await fa.asset(b))["accumulated_depreciation"]) == D("300")
    assert await fa.balance("1220") == before
    assert all(i["source"] != line["source"] for i in await fa.intake())
    claims = [c for c in await fa.entities("fa_src") if c["entity_id"] == f"fa:src:{line['source']}"]
    assert len(claims) == 1 and claims[0]["allocation"] == "link_depreciation"
    for asset_id in (a, b):
        _nbv_holds(await fa.asset(asset_id))
        await fa.fold_matches(asset_id)


async def test_s1_maintenance_on_asset_line_reclassified(fa):
    van = await fa.line("Delivery van")
    laptops = await fa.line("Laptop")
    asset_id = (await fa.register(van["source"], key="r1", mode="separate"))["assets"][0]["id"]
    done = await fa.operate("/register", {"source": laptops["source"], "mode": "maintenance", "asset_id": asset_id,
                                          "expense_account": "6950"}, key="mt")
    assert done["assets"] == [] and len(done["journals"]) == 1
    assert await fa.balance("6950") == D("4350") and await fa.balance("1210") == D("32000")
    logs = [m for m in await fa.entities("fa_maint") if m["asset_id"] == asset_id]
    assert len(logs) == 1 and logs[0]["source"] == laptops["source"] and D(logs[0]["amount"]) == D("4350")
    assert D((await fa.asset(asset_id))["cost"]) == D("32000")
    assert all(i["source"] != laptops["source"] for i in await fa.intake())


async def test_s1_pre_cutover_lines_not_in_intake(fa):
    settings = (await fa.ok(await fa.get("/setup")))["settings"]
    cutover = date.fromisoformat(settings["cutover"])
    early = await fa.journal((cutover - timedelta(days=20)).isoformat(),
                           [{"account": "1210", "debit": 100}, {"account": "3100", "credit": 100}])
    on_cutover = await fa.journal(cutover.isoformat(),
                                [{"account": "1210", "debit": 200}, {"account": "3100", "credit": 200}])
    after = await fa.journal(settings["first_managed_period"],
                           [{"account": "1210", "debit": 300}, {"account": "3100", "credit": 300}])
    sources = {i["source"] for i in await fa.intake()}
    assert f"{early}:0" not in sources and f"{on_cutover}:0" not in sources
    assert f"{after}:0" in sources
    r = await fa.post("/register", {"source": f"{early}:0", "class_id": (await fa.classes())["Vehicles"]["id"],
                                    "mode": "separate", "preview": True, "op_key": "pre"})
    assert r.status_code == 422 and "cutover" in r.json()["detail"].lower()


# ── Assets added by hand ─────────────────────────────────────────────────────


async def test_opening_asset_posts_nothing_and_refuses_after_cutover(fa):
    cutover = (await fa.ok(await fa.get("/setup")))["settings"]["cutover"]
    cls = (await fa.classes())["Buildings"]
    body = {"kind": "opening", "class_id": cls["id"], "name": "Warehouse", "available_date": "2020-01-01",
            "cost": "250000", "accumulated_depreciation": "40000"}
    done = await fa.operate("/assets", body, key="op")
    got = await fa.asset(done["assets"][0]["id"])
    assert D(got["cost"]) == D("250000") and D(got["accumulated_depreciation"]) == D("40000")
    assert D(got["nbv"]) == D("210000")
    (mv,) = [m for m in await fa.entities("fa_movement") if m["asset_id"] == got["id"]]
    assert mv["kind"] == "opening" and mv["posting_date"] == cutover and mv["contributions"] == []
    assert await fa.module_journals() == []
    late = (date.fromisoformat(cutover) + timedelta(days=1)).isoformat()
    r = await fa.post("/assets", {**body, "available_date": late, "preview": True, "op_key": "op2"})
    assert r.status_code == 422 and "Dated after the cutover" in r.json()["detail"]
    neg = await fa.post("/assets", {**body, "accumulated_depreciation": "300000", "preview": True, "op_key": "op3"})
    assert neg.status_code == 422


async def test_contributed_asset_posts_acquisition(fa):
    first = (await fa.ok(await fa.get("/setup")))["settings"]["first_managed_period"]
    cls = (await fa.classes())["Vehicles"]
    done = await fa.operate("/assets", {"kind": "contributed", "class_id": cls["id"], "name": "Owner's car",
                                        "available_date": first, "cost": "12000", "credit_account": "3100"}, key="c")
    (je_id,) = done["journals"]
    assert je_id.startswith("je:mod:celerp-fixed-assets:asset:")
    assert await fa.balance("1210") == D("48350") and await fa.balance("3100") == D("-12000")
    await fa.fold_matches(done["assets"][0]["id"])


async def test_adding_an_asset_by_hand_needs_the_right_permission(fa):
    """A physical record posts nothing, so manage_fixed_assets is enough; an opening or
    contributed asset carries cost, so it also needs manage_accounting."""
    from celerp.services.permissions import missing_permission_text

    operator, viewer = await fa.role_headers("operator"), await fa.role_headers("viewer")
    cls = (await fa.classes())["Buildings"]
    opening = {"kind": "opening", "class_id": cls["id"], "name": "Warehouse", "available_date": "2020-01-01",
               "cost": "1000", "preview": True, "op_key": "perm-1"}
    refused = await fa.post("/assets", opening, headers=operator)
    assert refused.status_code == 403 and refused.json()["detail"] == missing_permission_text("manage_accounting")
    physical = {"kind": "physical", "name": "Forklift", "preview": True, "op_key": "perm-2"}
    assert (await fa.post("/assets", physical, headers=operator)).status_code == 200
    blocked = await fa.post("/assets", physical, headers=viewer)
    assert blocked.status_code == 403 and blocked.json()["detail"] == missing_permission_text("manage_fixed_assets")


async def test_physical_record_then_financial_details(fa):
    done = await fa.operate("/assets", {"kind": "physical", "name": "Forklift"}, key="p")
    asset_id = done["assets"][0]["id"]
    got = await fa.asset(asset_id)
    assert got["status"] == "physical" and got["cost"] is None and got["class_id"] is None
    cutover = (await fa.ok(await fa.get("/setup")))["settings"]["cutover"]
    cls = (await fa.classes())["Plant and machinery"]
    await fa.operate(f"/assets/{asset_id}/financial-details", {
        "kind": "opening", "class_id": cls["id"], "available_date": "2022-06-01", "cost": "18000",
        "accumulated_depreciation": "5000"}, key="fd")
    got = await fa.asset(asset_id)
    assert got["status"] == "active" and D(got["cost"]) == D("18000") and got["number"] == "FA-0001"
    assert (await fa.ok(await fa.get(f"/assets/{asset_id}/movements")))["items"][0]["posting_date"] == cutover
    await fa.fold_matches(asset_id)


async def test_asset_list_filters_and_pagination(fa):
    van = await fa.line("Delivery van")
    laptops = await fa.line("Laptop")
    await fa.register(van["source"], key="r1", mode="separate")
    await fa.register(laptops["source"], key="r2", class_name="Computer equipment", mode="separate")
    every = await fa.ok(await fa.get("/assets"))
    assert every["total"] == 4
    cls = (await fa.classes())["Computer equipment"]
    only = await fa.ok(await fa.get("/assets", class_id=cls["id"]))
    assert only["total"] == 3
    page = await fa.ok(await fa.get("/assets", limit=2, offset=0))
    assert len(page["items"]) == 2 and page["total"] == 4
    found = await fa.ok(await fa.get("/assets", q="van"))
    assert [a["name"] for a in found["items"]] == ["Delivery van"]


# ── Invariants ───────────────────────────────────────────────────────────────


async def test_invariant_registered_cost_equals_source_and_ledger(fa):
    """Source cost equals registered cost, and the class accounts hold what the register holds."""
    for n, (description, cls, mode) in enumerate((("Delivery van", "Vehicles", "separate"),
                                                  ("Laptop", "Computer equipment", "separate"),
                                                  ("Office fit-out", "Assets under construction", "separate"))):
        line = await fa.line(description)
        done = await fa.register(line["source"], key=f"inv-{n}", class_name=cls, mode=mode)
        costs = [D((await fa.asset(a["id"]))["cost"]) for a in done["assets"]]
        assert sum(costs) == D(line["amount"])
    register = {"1210": D(0), "1240": D(0)}
    for a in (await fa.ok(await fa.get("/assets")))["items"]:
        cls = next(c for c in (await fa.classes()).values() if c["id"] == a["class_id"])
        register[cls["asset_account"]] += D(a["cost"])
        _nbv_holds(a)
        await fa.fold_matches(a["id"])
    assert register == {"1210": await fa.balance("1210"), "1240": await fa.balance("1240")}
