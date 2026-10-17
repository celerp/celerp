# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Who reads the register's money.

Amounts are financial: costs, values, prices, depreciation, journal lines and the files
that show them (bills, valuations, insurance and disposal papers). A role that views
fixed assets without view_financial_reports reads every non-financial fact about an
asset, on every surface, and none of its amounts: not in the asset, its movements, sale,
corrections, reviews, reminders or maintenance, not in a page, a print or a CSV, and not
in a notice told to the whole company. Taking the key away applies to the next request.

The check is by value, not by field name: every amount the books hold for the demo
company's assets is looked for in everything such a reader is given.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from fa_fixtures import FA
from test_files_tags import PNG, _file_id, _registered_van, _ui
from test_remeasure import _impair
from test_runs import _contributed, _managed_from, _run, _tick
from test_sales import _finalize, _sell
from test_transfer import _location, _move

D = Decimal
INSURED, ASKED, PRICE = "23456.78", "13579.24", "9876.54"
FINANCIAL_TAGS = ("bills", "valuations", "insurance", "disposal")
_NUMBER = re.compile(r"(?<![\w.:/-])\d[\d,]*(?:\.\d+)?(?![\w:/-])")


def _amounts(text: str) -> set[Decimal]:
    out = set()
    for m in _NUMBER.findall(text):
        try:
            out.add(D(m.replace(",", "")).normalize())
        except ArithmeticError:
            pass
    return out


async def _company(fa: FA) -> dict:
    """The demo company with a registered van and a contributed car taken through a run,
    an impairment, a sale, an insurance reminder, maintenance, a correction request and
    a prepared run; its files of each tag. Returns the ids and the amounts it holds."""
    await _managed_from(fa)
    van, _ = await _registered_van(fa)
    car = await _contributed(fa, cost="12345.67")
    await _run(fa, "2026-03")
    await _impair(fa, van, "2026-03-31", "25000")
    files = {tag: await _file_id(fa, van, name=f"{tag}.pdf", tag=tag) for tag in FINANCIAL_TAGS}
    files["photos"] = await _file_id(fa, van, name="van.png", content=PNG, mime="image/png", tag="photos")
    reminder = await fa.ok(await fa.post(f"/assets/{van}/reminders", {
        "kind": "insurance", "insurer": "Mutual", "policy": "P-77", "insured_value": INSURED, "due": "2026-12-31"}))
    maint = await fa.ok(await fa.post(f"/assets/{van}/maintenance", {"date": "2026-03-10", "work": "Oil change"}))
    operator = await fa.role_headers("operator")
    asked = await fa.ok(await fa.post(f"/assets/{car}/corrections", {
        "changes": {"cost": ASKED}, "reason": "The invoice was higher"}, headers=operator))
    invoice = await _sell(fa, car, price=PRICE)
    await _finalize(fa, invoice)
    await _tick(fa, date(2026, 5, 2))

    secrets = {D(x) for x in (INSURED, ASKED, PRICE)}
    for je in await fa.module_journals():
        for line in je["entries"]:
            secrets |= {D(str(line.get(k) or 0)) for k in ("debit", "credit")}
    for a in (van, car):
        got = await fa.asset(a)
        secrets |= {D(str(got[k])) for k in ("cost", "accumulated_depreciation", "accumulated_impairment", "nbv")
                    if got.get(k) is not None}
    for r in (await fa.ok(await fa.get("/runs")))["items"]:
        secrets.add(D(r["total"]))
    secrets = {s.normalize() for s in secrets if abs(s) >= 100}
    assert D("32000") in secrets and D(PRICE) in secrets
    return {"van": van, "car": car, "files": files, "reminder": reminder["id"], "maint": maint["id"],
            "correction": asked["id"], "invoice": invoice, "secrets": secrets}


def _api_reads(c: dict) -> list[str]:
    van, car = c["van"], c["car"]
    reads = ["/fixed-assets/api/assets", "/fixed-assets/api/corrections", f"/fixed-assets/api/corrections/{c['correction']}",
             "/fixed-assets/api/reviews", "/fixed-assets/api/reminders", f"/fixed-assets/api/reminders/{c['reminder']}",
             f"/fixed-assets/api/maintenance/{c['maint']}", "/fixed-assets/api/runs", "/fixed-assets/api/attention",
             "/fixed-assets/api/year-end?year=2026", "/fixed-assets/api/setup", "/fixed-assets/api/classes"]
    for a in (van, car):
        reads += [f"/fixed-assets/api/assets/{a}{tail}" for tail in
                  ("", "/movements", "/sale", "/reviews", "/upkeep", "/files", "/components", "/correct-fields")]
    return reads


def _pages(c: dict) -> list[str]:
    van, car = c["van"], c["car"]
    return ["/fixed-assets", f"/fixed-assets/assets/{van}", f"/fixed-assets/assets/{car}", "/fixed-assets/corrections",
            f"/fixed-assets/corrections/{c['correction']}", "/fixed-assets/runs", "/fixed-assets/runs/2026-04",
            "/fixed-assets/reviews", "/fixed-assets/reminders", "/fixed-assets/year-end?year=2026",
            "/fixed-assets/year-end/print?year=2026", "/fixed-assets/checks/not-seen.csv?since=2026-01-01",
            "/fixed-assets/checks/not-seen/print?since=2026-01-01", f"/fixed-assets/assets/{van}/files/_section",
            "/fixed-assets/reports/schedule.csv", "/fixed-assets/reports/schedule/print"]


async def _leaks(fa: FA, c: dict, headers: dict) -> list[tuple[str, set]]:
    """Every read, page and notice the member is given, with the amounts in it that the
    books hold."""
    found = []
    for path in _api_reads(c) + ["/notifications?limit=100"]:
        r = await fa.client.get(path, headers=headers)
        found.append((path, _amounts(r.text) & c["secrets"]))
    for path in _pages(c):
        r = await _ui(fa, "GET", path, headers)
        found.append((path, _amounts(r.text) & c["secrets"]))
    return [(p, s) for p, s in found if s]


def _listed(leaks: list[tuple[str, set]]) -> str:
    return "\n".join(f"{path}: {sorted(str(a) for a in found)}" for path, found in leaks)


async def _downloads(fa: FA, c: dict, headers: dict) -> dict[str, int]:
    return {tag: (await fa.client.get(f"/fixed-assets/api/assets/{c['van']}/files/{fid}", headers=headers)).status_code
            for tag, fid in c["files"].items()}


async def _revoke(fa: FA, role: str, granted: bool) -> None:
    await fa.ok(await fa.client.patch("/companies/me/role-permissions", headers=fa.h, json={
        "perm_key": "view_financial_reports", "role_key": role, "granted": granted}))


async def test_f02_no_amount_reaches_a_reader_without_financial_reports(fa):
    c = await _company(fa)
    operator = await fa.role_headers("operator")
    leaks = await _leaks(fa, c, operator)
    assert not leaks, _listed(leaks)

    # What it may read stays readable.
    van = (await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['van']}", headers=operator)))
    assert van["number"] and van["name"] and van["cost"] is None and van["nbv"] is None
    assert [f["document_tag"] for f in van["files"]] == ["photos"]
    listed = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['van']}/files", headers=operator))
    assert [f["document_tag"] for f in listed["files"]] == ["photos"] and listed["linked"] == []
    assert await _downloads(fa, c, operator) == {**dict.fromkeys(FINANCIAL_TAGS, 403), "photos": 200}
    reminder = await fa.ok(await fa.client.get(f"/fixed-assets/api/reminders/{c['reminder']}", headers=operator))
    assert (reminder["insurer"], reminder["policy"], reminder["due"], reminder["insured_value"]) == (
        "Mutual", "P-77", "2026-12-31", None)
    sale = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['car']}/sale", headers=operator))
    assert sale["invoice_id"] == c["invoice"] and sale["proceeds"] is None
    corr = await fa.ok(await fa.client.get(f"/fixed-assets/api/corrections/{c['correction']}", headers=operator))
    assert corr["state"] == "requested" and list(corr["changes"]) == ["cost"] and corr["changes"]["cost"] is None
    moves = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['van']}/movements", headers=operator))
    assert moves["total"] >= 2 and all(m["kind"] and m["posting_date"] for m in moves["items"])
    # The company-wide depreciation notice is still told, without its total.
    notes = (await fa.ok(await fa.client.get("/notifications", headers=operator)))["items"]
    assert any(n["title"] == "Depreciation ready to post" for n in notes)


async def test_f02_a_manager_reads_the_money_until_the_key_is_taken_away(fa):
    c = await _company(fa)
    manager = await fa.role_headers("manager")
    seen = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['van']}", headers=manager))
    assert D(seen["cost"]) == D("32000") and len(seen["files"]) == 5
    assert await _downloads(fa, c, manager) == dict.fromkeys([*FINANCIAL_TAGS, "photos"], 200)
    sale = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['car']}/sale", headers=manager))
    assert D(sale["proceeds"]) == D(PRICE)

    # Same session, the key taken away: the next request reads no amount.
    await _revoke(fa, "manager", False)
    leaks = await _leaks(fa, c, manager)
    assert not leaks, _listed(leaks)
    assert await _downloads(fa, c, manager) == {**dict.fromkeys(FINANCIAL_TAGS, 403), "photos": 200}
    # Given back, the money reads again.
    await _revoke(fa, "manager", True)
    seen = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{c['van']}", headers=manager))
    assert D(seen["cost"]) == D("32000")


async def test_f02_a_custodian_without_asset_access_reads_only_their_assets_without_money(fa):
    c = await _company(fa)
    uid, viewer = await fa.member("viewer")
    await _move(fa, c["van"], date="2026-04-02", location_id=await _location(fa),
                custodian={"user_id": str(uid)})
    leaks = await _leaks(fa, c, viewer)
    assert not leaks, _listed(leaks)
    mine = await fa.ok(await fa.client.get("/fixed-assets/api/mine", headers=viewer))
    assert [a["id"] for a in mine["items"]] == [c["van"]]
    assert [f["document_tag"] for f in mine["items"][0]["files"]] == ["photos"]
    for tag, fid in c["files"].items():
        got = await fa.client.get(f"/fixed-assets/api/mine/{c['van']}/files/{fid}", headers=viewer)
        assert got.status_code == (200 if tag == "photos" else 404), tag
    assert (await fa.client.get(f"/fixed-assets/api/assets/{c['van']}", headers=viewer)).status_code == 403


async def test_f02_amounts_reach_the_owner_unchanged(fa):
    """The neighbour: the owner still reads every amount the redaction hides."""
    c = await _company(fa)
    text = "".join([(await fa.client.get(p, headers=fa.h)).text for p in _api_reads(c)])
    assert {D("32000"), D(PRICE), D(INSURED), D(ASKED)} <= _amounts(text)
    assert await _downloads(fa, c, fa.h) == dict.fromkeys([*FINANCIAL_TAGS, "photos"], 200)
