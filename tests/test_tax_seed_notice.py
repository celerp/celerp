# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The startup pass that tells a company its stored tax rows match a retired seed."""
from __future__ import annotations

import copy
import json
import logging
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from celerp.models.company import Company
from celerp.models.notification import Notification
from celerp.services.company_lock import locked_company

from test_tax_seeds import _OLD_ET

pytestmark = pytest.mark.asyncio


async def _company(session, *, sales: list[dict] | None = None, purchasing: list[dict] | None = None,
                   active: bool = True, staged: bool = False) -> uuid.UUID:
    settings: dict = {}
    if sales is not None:
        settings["taxes"] = copy.deepcopy(sales)
    if purchasing is not None:
        settings["purchasing_taxes"] = copy.deepcopy(purchasing)
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="Seed notice co", slug=f"seed-{company_id.hex[:8]}", settings=settings,
                        is_active=active, is_migration_staged=staged))
    await session.commit()
    return company_id


async def _run(session) -> None:
    from celerp.services.tax_seed_notice import run_tax_seed_notice

    await run_tax_seed_notice(session)
    await session.commit()


async def _notices(session, company_id) -> dict[str, Notification]:
    """The company's tax seed notices, keyed by their retired-seed entry."""
    rows = (await session.execute(
        select(Notification).where(Notification.company_id == company_id))).scalars().all()
    prefix = "notice.tax_seed."
    return {n.i18n["title"][len(prefix):-len(".title")]: n for n in rows
            if (n.i18n or {}).get("title", "").startswith(prefix)}


async def test_tax_seed_notice_one_per_entry_and_idempotent(session):
    from celerp.tax_regimes import TAX_REGIMES

    old = await _company(session, sales=_OLD_ET)
    fresh = await _company(session, sales=TAX_REGIMES["TH"]["taxes"])

    await _run(session)
    notices = await _notices(session, old)
    assert set(notices) == {"et-wht-2025", "et-tot-2025"}
    assert all(n.category == "accounting" and n.priority == "high" for n in notices.values())
    assert all(n.i18n["body"].endswith(".body") for n in notices.values())
    assert await _notices(session, fresh) == {}

    await _run(session)
    assert {k: n.id for k, n in (await _notices(session, old)).items()} == {k: n.id for k, n in notices.items()}


async def test_tax_seed_notice_skips_a_company_staged_for_a_migration(session):
    """A company staged for a migration is left exactly as it was; a deactivated one is still told."""
    staged = await _company(session, sales=_OLD_ET, active=False, staged=True)
    paused = await _company(session, sales=_OLD_ET, active=False)

    await _run(session)
    assert await _notices(session, staged) == {}
    assert set(await _notices(session, paused)) == {"et-wht-2025", "et-tot-2025"}


async def test_tax_seed_notice_failure_is_logged_and_start_continues(monkeypatch, caplog):
    from test_main_lifespan import _boot_in_order

    monkeypatch.setattr("celerp.services.tax_seed_notice.run_tax_seed_notice",
                        AsyncMock(side_effect=RuntimeError("tax rows unreadable")))
    with caplog.at_level(logging.ERROR, logger="celerp.main"):
        order, _notify, app = await _boot_in_order(
            monkeypatch, AsyncMock(return_value={"changed": False, "rebuilt": False, "current": True}))

    assert "tax rows unreadable" in caplog.text
    assert app.state.data_current is True
    assert "cogs_backfill" in order


async def test_one_company_that_fails_leaves_every_other_company_told(session, monkeypatch, caplog):
    import celerp.services.tax_seed_notice as tax_seed_notice

    failing, told = await _company(session, sales=_OLD_ET), await _company(session, sales=_OLD_ET)
    real = tax_seed_notice.reconcile_tax_seed_notices

    async def _reconcile(sess, company_id, settings):
        await real(sess, company_id, settings)
        if company_id == failing:
            raise RuntimeError("stored settings unreadable")

    monkeypatch.setattr(tax_seed_notice, "reconcile_tax_seed_notices", _reconcile)
    with caplog.at_level(logging.ERROR, logger="celerp.services.tax_seed_notice"):
        await _run(session)

    assert str(failing) in caplog.text and "stored settings unreadable" in caplog.text
    assert await _notices(session, failing) == {}
    assert set(await _notices(session, told)) == {"et-wht-2025", "et-tot-2025"}


async def test_tax_seed_notice_clears_when_row_fixed(session):
    company_id = await _company(session, sales=_OLD_ET)
    await _run(session)
    assert set(await _notices(session, company_id)) == {"et-wht-2025", "et-tot-2025"}

    company = await locked_company(session, company_id)
    fixed = [r for r in copy.deepcopy(_OLD_ET) if r["name"] != "WHT 2%"]
    fixed.append({"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False, "is_compound": False})
    company.settings = {**company.settings, "taxes": fixed}
    await session.commit()

    await _run(session)
    assert set(await _notices(session, company_id)) == {"et-tot-2025"}


async def test_tax_seed_notice_links_to_the_list_that_still_holds_the_row(session):
    """WHT 2% in both lists: fixed in the sales list, the notice stands and now links to the
    purchasing list, where the row still is. It is the same notice, read or not."""
    vat = [r for r in _OLD_ET if r["name"] == "VAT 15%"]
    company_id = await _company(session, sales=_OLD_ET, purchasing=_OLD_ET)
    await _run(session)
    first = (await _notices(session, company_id))["et-wht-2025"]
    assert first.action_url == "/settings/sales?tab=taxes"

    company = await locked_company(session, company_id)
    company.settings = {**company.settings, "taxes": vat}
    await session.commit()
    await _run(session)
    session.expire_all()
    notices = await _notices(session, company_id)
    assert set(notices) == {"et-wht-2025", "et-tot-2025"}
    assert notices["et-wht-2025"].id == first.id
    assert notices["et-wht-2025"].action_url == "/settings/purchasing?tab=taxes"


# What each retired entry's notice tells the company to do instead. A new entry fails the test
# below until its replacement is declared here and named in its notice.
_REPLACEMENT = {"in-default-2025": "IGST 18%", "et-wht-2025": "3%", "et-tot-2025": "Remove",
                "ca-qst-compound": "Set Compound to No", "my-sst-6": "Sales Tax at 10%"}


async def test_each_retired_seed_notice_names_its_rows_and_the_replacement():
    from celerp.tax_regimes import RETIRED_SEED_ROWS
    from ui.i18n import t

    assert {entry["id"] for entry in RETIRED_SEED_ROWS} == set(_REPLACEMENT)
    for entry in RETIRED_SEED_ROWS:
        body = t(f"notice.tax_seed.{entry['id']}.body", "en")
        assert [row["name"] for row in entry["rows"] if row["name"] not in body] == [], entry["id"]
        assert _REPLACEMENT[entry["id"]] in body, entry["id"]


async def test_tax_seed_notice_links_purchasing_tab_for_purchasing_match(session):
    vat = [r for r in _OLD_ET if r["name"] == "VAT 15%"]
    wht = [r for r in _OLD_ET if r["name"] == "WHT 2%"]
    purchasing_only = await _company(session, sales=vat, purchasing=vat + wht)
    sales_side = await _company(session, sales=_OLD_ET)

    await _run(session)
    notices = await _notices(session, purchasing_only)
    assert set(notices) == {"et-wht-2025"}
    assert notices["et-wht-2025"].action_url == "/settings/purchasing?tab=taxes"
    assert (await _notices(session, sales_side))["et-wht-2025"].action_url == "/settings/sales?tab=taxes"


async def test_tax_seed_notice_never_changes_company_taxes(session):
    vat = [r for r in _OLD_ET if r["name"] == "VAT 15%"]
    company_id = await _company(session, sales=_OLD_ET, purchasing=vat + _OLD_ET[-1:])
    before = json.dumps((await session.get(Company, company_id)).settings, sort_keys=True)

    await _run(session)
    session.expire_all()
    assert await _notices(session, company_id)
    assert json.dumps((await session.get(Company, company_id)).settings, sort_keys=True) == before


async def _register(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "Seed notice co", "email": f"sn-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Admin", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _bell(client, h) -> set[str]:
    """The tax seed notices the company's bell shows, by retired-seed entry."""
    r = await client.get("/notifications", headers=h, params={"limit": 100})
    assert r.status_code == 200, r.text
    prefix = "notice.tax_seed."
    return {n["i18n"]["title"][len(prefix):-len(".title")] for n in r.json()["items"]
            if ((n.get("i18n") or {}).get("title") or "").startswith(prefix)}


def _record(row: dict) -> dict:
    return {"entity_id": "company", "event_type": "tax.import", "source": "import",
            "idempotency_key": f"tax-{uuid.uuid4().hex[:8]}", "data": row}


@pytest.mark.parametrize("path", ["/companies/me/taxes", "/companies/me/purchasing-taxes"])
async def test_saving_a_tax_list_brings_its_notices_up_to_date_at_once(client, path):
    """No restart needed: saving a list holding a retired row raises its notice, and saving it
    fixed clears the notice the same moment."""
    h = await _register(client)
    assert (await client.patch(path, headers=h, json={"taxes": _OLD_ET})).status_code == 200
    assert await _bell(client, h) == {"et-wht-2025", "et-tot-2025"}

    fixed = [r for r in _OLD_ET if r["name"] not in ("WHT 2%", "TOT 2%", "TOT 10%")]
    assert (await client.patch(path, headers=h, json={"taxes": fixed})).status_code == 200
    assert await _bell(client, h) == set()


@pytest.mark.parametrize("path", ["/companies/me/taxes/import/batch",
                                  "/companies/me/purchasing-taxes/import/batch"])
async def test_importing_a_retired_tax_row_raises_its_notice_at_once(client, path):
    h = await _register(client)
    sst = {"name": "SST 6%", "rate": 6.0, "tax_type": "both"}
    r = await client.post(path, headers=h, json={"records": [_record(sst)]})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    assert await _bell(client, h) == {"my-sst-6"}


async def _list_notice(session, company_id) -> Notification | None:
    session.expire_all()
    rows = (await session.execute(select(Notification).where(Notification.company_id == company_id))).scalars().all()
    return next((n for n in rows if (n.i18n or {}).get("title") == "notice.list_tax_rule.title"), None)


async def test_quotations_saved_under_the_earlier_tax_rule_are_told_and_never_changed(client, session):
    """A quotation an earlier version saved taxed its lines before the header discount. Its stored
    totals are the company's record, so the startup pass only reads them: it names the quotations
    the document rule would total differently and leaves them as they are. Saving one with no
    change to its money keeps its totals and the notice; Recalculate total clears it."""
    from celerp.models.projections import Projection
    from test_tax_acceptance_matrix import _demo_company

    auth, _ = await _demo_company(client, session, "USD")
    r = await client.post("/lists", headers=auth["headers"], json={
        "list_type": "quotation", "currency": "USD", "discount": 10, "discount_type": "flat",
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": 100,
                        "taxes": [{"code": "VAT 10%", "rate": 10, "order": 0, "is_compound": False, "label": ""}]}]})
    assert r.status_code == 200, r.text
    lst = r.json()["id"]
    row = await session.get(Projection, (auth["company_id"], lst))
    earlier = {**row.state, "tax_amount": 10.0, "total": 100.0}  # as the earlier list rule saved it
    row.state = earlier
    await session.commit()

    await _run(session)
    await _run(session)
    notice = await _list_notice(session, auth["company_id"])
    assert notice is not None and notice.action_url == "/lists"
    assert notice.i18n["params"] == {"count": 1, "refs": earlier["ref_id"]}
    session.expire_all()
    assert (await session.get(Projection, (auth["company_id"], lst))).state == earlier

    r = await client.patch(f"/lists/{lst}", headers=auth["headers"], json={
        "fields_changed": {"notes": {"old": None, "new": "Saved again"}}})
    assert r.status_code == 200, r.text
    await _run(session)
    assert await _list_notice(session, auth["company_id"]) is not None
    session.expire_all()
    assert (await session.get(Projection, (auth["company_id"], lst))).state["total"] == 100.0

    r = await client.post(f"/lists/{lst}/recalculate-total", headers=auth["headers"])
    assert r.status_code == 200, r.text
    await _run(session)
    assert await _list_notice(session, auth["company_id"]) is None


async def _bell_links(client, h) -> dict[str, str]:
    """The link of each tax seed notice the company's bell shows, by retired-seed entry."""
    r = await client.get("/notifications", headers=h, params={"limit": 100})
    assert r.status_code == 200, r.text
    prefix = "notice.tax_seed."
    return {n["i18n"]["title"][len(prefix):-len(".title")]: n["action_url"] for n in r.json()["items"]
            if ((n.get("i18n") or {}).get("title") or "").startswith(prefix)}


async def test_saving_the_sales_list_fixed_moves_the_notice_link_to_the_purchasing_list(client):
    """The retired rows saved in both lists, then fixed in the sales list: the same notices stand
    and now link to the purchasing list, where the rows still are."""
    h = await _register(client)
    sales, purchase = "/settings/sales?tab=taxes", "/settings/purchasing?tab=taxes"
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": _OLD_ET})).status_code == 200
    assert (await client.patch("/companies/me/purchasing-taxes", headers=h, json={"taxes": _OLD_ET})).status_code == 200
    assert await _bell_links(client, h) == {"et-wht-2025": sales, "et-tot-2025": sales}

    fixed = [r for r in _OLD_ET if r["name"] not in ("WHT 2%", "TOT 2%", "TOT 10%")]
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": fixed})).status_code == 200
    assert await _bell_links(client, h) == {"et-wht-2025": purchase, "et-tot-2025": purchase}


async def test_a_company_past_the_notification_cap_keeps_only_its_newest(client, monkeypatch):
    """Two notices raised by one save with the cap at one: the older is pruned in the same request,
    and the one kept is the newer, raised second in RETIRED_SEED_ROWS order."""
    monkeypatch.setattr("celerp.notifications.service.MAX_PER_COMPANY", 1)
    h = await _register(client)
    assert (await client.patch("/companies/me/taxes", headers=h, json={"taxes": _OLD_ET})).status_code == 200
    assert await _bell(client, h) == {"et-tot-2025"}
