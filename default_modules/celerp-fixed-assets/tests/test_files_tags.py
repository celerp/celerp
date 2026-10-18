# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Files and tags on an asset, printing asset tags through labels, the staff custodian's
own assets, and physical checks by tag.

Who sees what follows the role's keys: money and the files of the bill or invoice
behind an asset only with view_financial_reports; a custodian, whatever their role,
sees the assets in their care without money, and only the photos and manuals on them.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from celerp.services.business_time import business_date_of
from fa_fixtures import FA
from test_disposal import _dispose
from test_runs import _contributed, _managed_from, _race_company
from test_transfer import _location, _move

pytestmark = pytest.mark.asyncio

PDF = b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n"
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _upload(fa: FA, asset_id: str, name: str = "warranty.pdf", content: bytes = PDF,
                  mime: str = "application/pdf", tag: str = "warranties", headers=None):
    return await fa.client.post(f"/fixed-assets/api/assets/{asset_id}/files", headers=headers or fa.h,
                                files={"file": (name, content, mime)}, data={"document_tag": tag})


async def _file_id(fa: FA, asset_id: str, **kwargs) -> str:
    r = await _upload(fa, asset_id, **kwargs)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _tag(fa: FA, asset_id: str, tag: str | None, headers=None):
    asset = await fa.asset(asset_id)
    return await fa.patch(f"/assets/{asset_id}", {"changes": {"tag": tag}, "previous": {"tag": asset.get("tag")}},
                          headers=headers)


async def _grant(fa: FA, key: str, roles: list[str]) -> None:
    from celerp.services.company_lock import locked_company

    company = await locked_company(fa.session, fa.auth["company_id"])
    grants = dict((company.settings or {}).get("role_grants") or {})
    grants[key] = roles
    company.settings = {**(company.settings or {}), "role_grants": grants}
    await fa.session.commit()


async def _labels(fa: FA, on: bool) -> None:
    from celerp.modules.registry import get_enabled, set_enabled
    from celerp.services.company_lock import locked_company

    company = await locked_company(fa.session, fa.auth["company_id"])
    enabled = get_enabled(company.settings) | {"celerp-fixed-assets", "celerp-accounting", "celerp-docs",
                                               "celerp-inventory"}
    enabled = enabled | {"celerp-labels"} if on else enabled - {"celerp-labels"}
    company.settings = set_enabled(dict(company.settings or {}), enabled)
    await fa.session.commit()


async def _ui(fa: FA, method: str, path: str, headers: dict, **kwargs):
    """A UI request made by the member whose API headers are given."""
    return await FA(fa.client, fa.session, {**fa.auth, "headers": headers})._ui(method, path, **kwargs)


async def _registered_van(fa: FA) -> tuple[str, str]:
    """The demo van registered from its bill, with a scan attached to the bill: (asset, bill)."""
    van = await fa.line("Delivery van")
    bill = van["source"].rsplit(":", 1)[0]
    r = await fa.client.post(f"/docs/{bill}/files", headers=fa.h,
                             files={"file": ("van-invoice.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    done = await fa.register(van["source"], key="van", mode="separate", available_date="2026-02-01")
    return done["assets"][0]["id"], bill


# ── Files ────────────────────────────────────────────────────────────────────


async def test_files_upload_tag_describe_download_delete(fa):
    car = await _contributed(fa)
    fid = await _file_id(fa, car)
    files = (await fa.asset(car))["files"]
    assert [(f["id"], f["filename"], f["document_tag"]) for f in files] == [(fid, "warranty.pdf", "warranties")]

    r = await fa.client.post(f"/fixed-assets/api/assets/{car}/files/{fid}/tag", headers=fa.h,
                             data={"document_tag": "manuals"})
    assert r.status_code == 200, r.text
    r = await fa.client.patch(f"/fixed-assets/api/assets/{car}/files/{fid}/description", headers=fa.h,
                              data={"description": "Owner's manual"})
    assert r.status_code == 200, r.text
    (got,) = (await fa.asset(car))["files"]
    assert got["document_tag"] == "manuals" and got["description"] == "Owner's manual"

    download = await fa.client.get(f"/fixed-assets/api/assets/{car}/files/{fid}", headers=fa.h)
    assert download.status_code == 200 and download.content == PDF

    assert (await fa.client.delete(f"/fixed-assets/api/assets/{car}/files/{fid}", headers=fa.h)).status_code == 200
    assert (await fa.asset(car))["files"] == []
    gone = await fa.client.get(f"/fixed-assets/api/assets/{car}/files/{fid}", headers=fa.h)
    assert gone.status_code == 404


async def test_file_refusals_are_named(fa):
    car = await _contributed(fa)
    refused = await _upload(fa, car, name="setup.exe", content=b"MZ", mime="application/x-msdownload")
    assert refused.status_code == 413 and "application/x-msdownload" in refused.json()["detail"]
    bad_tag = await _upload(fa, car, tag="product_images")
    assert bad_tag.status_code == 422 and "product_images" in bad_tag.json()["detail"]
    viewer = await fa.role_headers("viewer")
    assert (await _upload(fa, car, headers=viewer)).status_code == 403
    assert (await fa.asset(car))["files"] == []


async def test_bill_files_listed_only_to_roles_that_see_money(fa):
    van, bill = await _registered_van(fa)
    linked = (await fa.ok(await fa.get(f"/assets/{van}/files")))["linked"]
    assert [(f["filename"], f["document"]) for f in linked] == [("van-invoice.pdf", bill)]

    operator = await fa.role_headers("operator")
    seen = await fa.ok(await fa.client.get(f"/fixed-assets/api/assets/{van}/files", headers=operator))
    assert seen["linked"] == []

    page = await fa.page(f"/fixed-assets/assets/{van}")
    assert page.status_code == 200 and "van-invoice.pdf" in page.text
    page = await _ui(fa, "GET", f"/fixed-assets/assets/{van}", operator)
    assert page.status_code == 200 and "van-invoice.pdf" not in page.text


async def test_files_card_on_the_asset_page(fa):
    from ui.i18n import t

    car = await _contributed(fa)
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert page.status_code == 200 and t("label.no_files_yet") in page.text
    await _file_id(fa, car)
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert "warranty.pdf" in page.text and t("file_tag.warranties") in page.text
    proxied = await fa.page(f"/fixed-assets/assets/{car}/files/{(await fa.asset(car))['files'][0]['id']}/download")
    assert proxied.status_code == 200 and proxied.content == PDF


# ── Tags and labels ──────────────────────────────────────────────────────────


async def test_tags_unique_among_the_company_assets(fa):
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    assert (await _tag(fa, car, "TAG-001")).status_code == 200
    taken = await _tag(fa, van, "tag-001")
    assert taken.status_code == 422
    assert "TAG-001" in taken.json()["detail"] and (await fa.asset(car))["number"] in taken.json()["detail"]
    assert (await _tag(fa, car, None)).status_code == 200
    assert (await _tag(fa, van, "TAG-001")).status_code == 200


async def test_label_rows_for_assets(fa):
    car = await _contributed(fa)
    yard = await _location(fa, "Yard")
    await _move(fa, car, date="2026-02-01", location_id=yard)
    await _tag(fa, car, "TAG-7")
    van = await _contributed(fa, name="Van")
    r = await fa.client.get("/api/labels/source-rows", headers=fa.h, params={"source": "fa_asset", "ids": f"{car},{van}"})
    assert r.status_code == 200, r.text
    rows = r.json()["rows"]
    number = (await fa.asset(car))["number"]
    assert rows[0] == {"id": car, "number": number, "tag": "TAG-7", "name": "Owner's car", "class": "Vehicles",
                       "location": "Yard", "in_service": "2026-01-15", "barcode": "TAG-7"}
    assert rows[1]["barcode"] == (await fa.asset(van))["number"] and rows[1]["tag"] is None
    assert "cost" not in rows[0]

    unknown = await fa.client.get("/api/labels/source-rows", headers=fa.h, params={"source": "nope", "ids": car})
    assert unknown.status_code == 404
    viewer = await fa.role_headers("viewer")
    refused = await fa.client.get("/api/labels/source-rows", headers=viewer, params={"source": "fa_asset", "ids": car})
    assert refused.status_code == 403
    # The first print, before anyone has made a template, uses the shipped asset tag.
    pdf = await fa.client.post("/api/labels/print-source", headers=fa.h, json={"source": "fa_asset", "ids": [car]})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


async def test_u49_the_first_print_tag_prints_on_the_shipped_asset_tag(fa):
    from celerp_labels.presets import PRESET_TEMPLATES
    from ui.i18n import t

    await _labels(fa, True)
    car = await _contributed(fa)
    await _tag(fa, car, "TAG-9")
    printed = await fa.page("/labels/print-source", source="fa_asset", ids=car, back=f"/fixed-assets/assets/{car}")
    assert printed.status_code == 200 and "TAG-9" in printed.text and "window.print()" in printed.text
    assert f"{t('fixed_assets.number')}: " in printed.text and "fixed_assets.number" not in printed.text
    assert f'href="/fixed-assets/assets/{car}"' in printed.text or f"'/fixed-assets/assets/{car}'" in printed.text

    # A role that may not manage labels prints on it too.
    operator = await fa.role_headers("operator")
    await _grant(fa, "manage_labels", ["owner", "admin", "manager"])
    again = await _ui(fa, "GET", "/labels/print-source", operator, params={"source": "fa_asset", "ids": car})
    assert "TAG-9" in again.text and "window.print()" in again.text

    # It is the company's own template, kept once and editable in Labels, beside the usual presets.
    names = [tpl["name"] for tpl in (await fa.client.get("/api/labels/templates", headers=fa.h)).json()["items"]]
    # Named like the presets beside it: what it is, then its size.
    assert names.count(f"{t('fixed_assets.asset_tag')} (50x25)") == 1, names
    assert {p["name"] for p in PRESET_TEMPLATES} <= set(names), names


async def test_print_tag_when_labels_are_off(fa):
    from ui.i18n import t

    car = await _contributed(fa)
    await _labels(fa, True)
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.print_tag") in page.text
    await _labels(fa, False)
    page = await fa.page(f"/fixed-assets/assets/{car}")
    assert t("fixed_assets.turn_on_labels") in page.text and t("modules.turn_on") in page.text
    operator = await fa.role_headers("operator")
    page = await _ui(fa, "GET", f"/fixed-assets/assets/{car}", operator)
    assert t("fixed_assets.ask_admin_labels") in page.text and t("modules.turn_on") not in page.text
    # The number can always be written on a tag by hand.
    assert (await fa.asset(car))["number"] in page.text


async def test_turning_labels_on_returns_to_the_asset(fa):
    car = await _contributed(fa)
    await _labels(fa, False)
    back = f"/fixed-assets/assets/{car}"
    r = await fa.submit("/modules/celerp-labels/enable", {"next": back})
    assert r.status_code == 200 and r.headers.get("HX-Redirect") in (back, "/modules")
    # A next that leaves the app is ignored: the modules panel answers as before.
    await _labels(fa, False)
    r = await fa.submit("/modules/celerp-labels/enable", {"next": "//elsewhere.example/x"})
    assert r.status_code == 200 and "HX-Redirect" not in r.headers


async def test_label_item_source_unchanged(fa):
    """Item labels still print from the item itself."""
    r = await fa.client.get("/items", headers=fa.h, params={"limit": 1})
    item = r.json()["items"][0]
    item_id = item.get("entity_id") or item.get("id")
    pdf = await fa.client.post(f"/api/labels/print/{item_id}", headers=fa.h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


async def test_contact_files_unchanged_after_helper_extract():
    """A contact's files list its own files and those on its documents, each pointing at its owner."""
    from fasthtml.common import to_xml

    from ui.routes.contacts import _files_section

    contact = {"files": [{"id": "f1", "filename": "nda.pdf", "uploaded_at": "2026-01-02T00:00:00Z"}]}
    docs = [{"entity_id": "doc:9", "ref_id": "INV-0009",
             "files": [{"id": "f2", "filename": "scan.pdf", "uploaded_at": "2026-01-03T00:00:00Z"},
                       {"id": "f1", "filename": "nda.pdf"}]}]
    html = to_xml(_files_section(contact, "contact:1", docs))
    assert "nda.pdf" in html and "scan.pdf" in html and "INV-0009" in html
    assert "/docs/doc:9/files/f2/download" in html and "/contacts/contact:1/files/f1/download" in html


# ── Custodian ────────────────────────────────────────────────────────────────


async def test_custodian_sees_own_assets_restricted(fa):
    car = await _contributed(fa)
    other = await _contributed(fa, name="Van")
    uid, custodian = await fa.member("viewer")
    await _move(fa, car, date="2026-02-01", custodian={"user_id": str(uid)})
    await _tag(fa, car, "TAG-3")
    photo = await _file_id(fa, car, name="car.png", content=PNG, mime="image/png", tag="photos")
    warranty = await _file_id(fa, car)

    mine = await fa.ok(await fa.client.get("/fixed-assets/api/mine", headers=custodian))
    assert [a["id"] for a in mine["items"]] == [car]
    got = await fa.ok(await fa.client.get(f"/fixed-assets/api/mine/{car}", headers=custodian))
    assert got["tag"] == "TAG-3" and got["name"] == "Owner's car"
    assert not {"cost", "nbv", "accumulated_depreciation", "sources", "class_id", "movements"} & set(got)
    assert [f["id"] for f in got["files"]] == [photo]
    ok = await fa.client.get(f"/fixed-assets/api/mine/{car}/files/{photo}", headers=custodian)
    assert ok.status_code == 200 and ok.content == PNG
    hidden = await fa.client.get(f"/fixed-assets/api/mine/{car}/files/{warranty}", headers=custodian)
    assert hidden.status_code == 404
    assert (await fa.client.get(f"/fixed-assets/api/mine/{other}", headers=custodian)).status_code == 404
    assert (await fa.client.get(f"/fixed-assets/api/assets/{car}", headers=custodian)).status_code == 403

    page = await _ui(fa, "GET", "/fixed-assets/mine", custodian)
    assert page.status_code == 200 and "TAG-3" in page.text and "Van" not in page.text
    nav = await _ui(fa, "GET", "/fixed-assets/mine/nav", custodian)
    assert 'href="/fixed-assets/mine"' in nav.text
    stranger = await fa.role_headers("viewer")
    assert 'href="/fixed-assets/mine"' not in (await _ui(fa, "GET", "/fixed-assets/mine/nav", stranger)).text
    # With nothing in their care, the page says how an asset comes to them and leads back.
    from ui.i18n import t
    none = (await _ui(fa, "GET", "/fixed-assets/mine", stranger)).text
    assert t("fixed_assets.mine_empty_hint") in none
    assert f'href="/" class="empty-state-cta-btn">{t("fixed_assets.mine_back")}' in none


async def test_custodian_confirms_and_reports_a_problem(fa):
    from celerp.models.notification import Notification

    car = await _contributed(fa)
    uid, custodian = await fa.member("viewer")
    manager_id, _ = await fa.member("manager")
    await _move(fa, car, date="2026-02-01", custodian={"user_id": str(uid)})
    asset = await fa.asset(car)
    await fa.ok(await fa.patch(f"/assets/{car}", {"changes": {"responsible_user_id": str(manager_id)},
                                                  "previous": {"responsible_user_id": asset.get("responsible_user_id")}}))

    confirmed = await fa.ok(await fa.client.post(f"/fixed-assets/api/mine/{car}/confirm", headers=custodian))
    assert confirmed["confirmed"]["user_id"] == str(uid) and confirmed["confirmed"]["date"] == business_date_of(None, None)

    r = await fa.client.post(f"/fixed-assets/api/mine/{car}/problems", headers=custodian,
                             data={"text": "Brake light out"}, files={"photo": ("light.png", PNG, "image/png")})
    assert r.status_code == 200, r.text
    asset = await fa.asset(car)
    assert asset["problems"][0]["text"] == "Brake light out" and asset["problems"][0]["user_id"] == str(uid)
    assert [f["document_tag"] for f in asset["files"]] == ["photos"]
    fa.session.expire_all()
    notes = list((await fa.session.execute(select(Notification).where(
        Notification.company_id == fa.auth["company_id"], Notification.category == "fixed_assets",
        Notification.user_id == manager_id))).scalars())
    assert len(notes) == 1 and "Brake light out" in notes[0].body and asset["number"] in notes[0].title

    empty = await fa.client.post(f"/fixed-assets/api/mine/{car}/problems", headers=custodian, data={"text": " "})
    assert empty.status_code == 422
    stranger = await fa.role_headers("viewer")
    assert (await fa.client.post(f"/fixed-assets/api/mine/{car}/confirm", headers=stranger)).status_code == 404
    responsible = await fa.patch(f"/assets/{car}", {"changes": {"responsible_user_id": str(uuid.uuid4())},
                                                    "previous": {"responsible_user_id": str(manager_id)}})
    assert responsible.status_code == 422


# ── Checks ───────────────────────────────────────────────────────────────────


async def test_s9_check_key_without_edit_keys(fa):
    await _grant(fa, "check_fixed_assets", ["owner", "admin", "manager", "operator", "viewer"])
    car = await _contributed(fa)
    await _tag(fa, car, "TAG-5")
    auditor_id, auditor = await fa.member("viewer")

    check = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=auditor, json={"tag": "tag-5"}))
    assert check["asset_id"] == car and check["user_id"] == str(auditor_id)
    assert check["date"] == business_date_of(None, None)
    edit = await fa.patch(f"/assets/{car}", {"changes": {"name": "X"}, "previous": {"name": "Owner's car"}},
                          headers=auditor)
    assert edit.status_code == 403
    moved = await fa.post(f"/assets/{car}/move", {"date": "2026-02-01", "custodian": {"name": "A", "not_user": True},
                                                 "preview": True, "op_key": "m"}, headers=auditor)
    assert moved.status_code == 403
    assert (await fa.post(f"/assets/{car}/dispose", {"date": "2026-03-01", "preview": True, "op_key": "d"},
                          headers=auditor)).status_code == 403

    unknown = await fa.client.post("/fixed-assets/api/checks", headers=auditor, json={"tag": "NOPE"})
    assert unknown.status_code == 422 and unknown.json()["detail"] == "No asset with tag NOPE."
    # Without the key, no checks.
    await _grant(fa, "check_fixed_assets", ["owner", "admin", "manager", "operator"])
    assert (await fa.client.post("/fixed-assets/api/checks", headers=auditor, json={"tag": "TAG-5"})).status_code == 403


async def test_checks_disposed_removal_and_not_seen_report(fa):
    await _managed_from(fa, "2026-01-01")
    car = await _contributed(fa)
    van = await _contributed(fa, name="Van")
    old = await _contributed(fa, name="Old printer")
    await _tag(fa, car, "TAG-1")
    await _tag(fa, old, "TAG-OLD")
    await _dispose(fa, old, "2026-03-31")

    disposed = await fa.client.post("/fixed-assets/api/checks", headers=fa.h, json={"tag": "TAG-OLD"})
    assert disposed.status_code == 422 and disposed.json()["detail"] == "Disposed on 2026-03-31."
    check = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=fa.h, json={"tag": "TAG-1"}))
    # Scanning the asset number works the same when the asset has no tag.
    van_number = (await fa.asset(van))["number"]
    await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=fa.h, json={"tag": van_number}))

    listed = await fa.ok(await fa.client.get("/fixed-assets/api/checks", headers=fa.h))
    assert [c["asset_id"] for c in listed["items"]] == [van, car]  # newest first

    today = business_date_of(None, None)
    report = await fa.ok(await fa.client.get("/fixed-assets/api/checks/not-seen", headers=fa.h,
                                             params={"since": today}))
    assert [r["id"] for r in report["items"]] == []
    other = await fa.role_headers("operator")
    assert (await fa.client.delete(f"/fixed-assets/api/checks/{check['id']}", headers=other)).status_code == 403
    assert (await fa.client.delete(f"/fixed-assets/api/checks/{check['id']}", headers=fa.h)).status_code == 200
    report = await fa.ok(await fa.client.get("/fixed-assets/api/checks/not-seen", headers=fa.h,
                                             params={"since": today}))
    assert [(r["id"], r["last_seen"]) for r in report["items"]] == [(car, None)]
    bad = await fa.client.get("/fixed-assets/api/checks/not-seen", headers=fa.h, params={"since": "soon"})
    assert bad.status_code == 422

    page = await fa.page("/fixed-assets/checks")
    assert page.status_code == 200 and van_number in page.text
    csv = await fa.page("/fixed-assets/checks/not-seen.csv", since=today)
    assert csv.status_code == 200 and (await fa.asset(car))["number"] in csv.text
    printed = await fa.page("/fixed-assets/checks/not-seen/print", since=today)
    assert printed.status_code == 200 and (await fa.asset(car))["number"] in printed.text


async def test_u40_a_repeat_scan_the_same_day_keeps_one_check_each_with_the_asset_name(fa):
    await _managed_from(fa, "2026-01-01")
    car = await _contributed(fa)
    await _tag(fa, car, "TAG-1")
    first = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=fa.h, json={"tag": "TAG-1"}))
    again = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=fa.h, json={"tag": "TAG-1"}))
    by_number = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=fa.h,
                                                 json={"tag": (await fa.asset(car))["number"]}))
    assert again["id"] == by_number["id"] == first["id"] and again["already"] is True and "already" not in first
    # Someone else who sees it the same day records their own check.
    other = await fa.role_headers("operator")
    theirs = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=other, json={"tag": "TAG-1"}))
    assert theirs["id"] != first["id"]
    listed = (await fa.ok(await fa.client.get("/fixed-assets/api/checks", headers=fa.h)))["items"]
    assert len(listed) == 2 and {c["name"] for c in listed} == {(await fa.asset(car))["name"]}
    # A removed check no longer counts: scanning again records a new one.
    await fa.ok(await fa.client.delete(f"/fixed-assets/api/checks/{first['id']}", headers=fa.h))
    anew = await fa.ok(await fa.client.post("/fixed-assets/api/checks", headers=fa.h, json={"tag": "TAG-1"}))
    assert anew["id"] != first["id"] and "already" not in anew


async def test_u40_a_repeat_scan_says_so_as_a_notice_not_a_success(fa):
    import json

    from ui.i18n import t

    await _managed_from(fa, "2026-01-01")
    car = await _contributed(fa)
    await _tag(fa, car, "TAG-1")
    number = (await fa.asset(car))["number"]
    toasts = []
    for _ in range(2):
        r = await _ui(fa, "POST", "/fixed-assets/checks", fa.h, data={"tag": "TAG-1"})
        toasts.append(json.loads(r.headers["HX-Trigger"])["celerpToast"])
    assert toasts == [{"message": t("fixed_assets.check_recorded", number=number), "type": "success"},
                      {"message": t("fixed_assets.check_already", number=number), "type": "info"}], toasts


async def test_f09_check_waits_for_a_tag_being_moved(committed_engine):
    """A scan while the tag is being moved to another asset records the asset that carries
    the tag once the move commits, never the one it is leaving."""
    from celerp_fixed_assets import checks, operations, setup
    from celerp.services.company_lock import lock_company

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    cid, uid = await _race_company(factory)
    async with factory() as s:
        cls = next(c for c in await setup.classes(s, cid) if c["name"] == "Vehicles")
        body = {"kind": "contributed", "class_id": cls["id"], "name": "Van", "available_date": "2026-01-15",
                "cost": "9000", "credit_account": "3100", "op_key": "van"}
        preview = await operations.create_asset(s, cid, uid, {**body, "preview": True})
        van = (await operations.create_asset(s, cid, uid, {**body, "fingerprint": preview["fingerprint"]}))["assets"][0]["id"]
        await s.commit()
    async with factory() as s:
        (car,) = [a for a, st in await checks._entities(s, cid, "fa_asset") if st.get("name") == "Car"]
        await operations.update_asset(s, cid, uid, car, {"changes": {"tag": "T-1"}, "previous": {"tag": None}})
        await s.commit()

    async def scan(tag: str) -> dict:
        async with factory() as s:
            return await checks.record_check(s, cid, uid, tag)

    async with factory() as mover:
        await lock_company(mover, cid)
        await operations.update_asset(mover, cid, uid, car, {"changes": {"tag": "T-2"}, "previous": {"tag": "T-1"}})
        await operations.update_asset(mover, cid, uid, van, {"changes": {"tag": "T-1"}, "previous": {"tag": None}})
        pending = asyncio.create_task(scan("T-1"))
        await asyncio.sleep(0.5)
        assert not pending.done(), "the check resolved the tag while it was being moved"
        await mover.commit()
    check = await asyncio.wait_for(pending, 10)
    assert check["asset_id"] == van

    # The tag the car now carries finds the car; an unknown tag is still refused, and the
    # check is stored under the asset it names.
    assert (await scan("t-2"))["asset_id"] == car
    with pytest.raises(Exception) as unknown:
        await scan("T-3")
    assert getattr(unknown.value, "status_code", None) == 422
    async with factory() as s:
        stored = {c["id"]: c["asset_id"] for c in await checks.live_checks(s, cid)}
    assert stored[check["id"]] == van and sorted(stored.values()) == sorted([van, car])


# ── Who sees what ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("role", ["viewer", "operator", "manager", "admin", "owner"])
@pytest.mark.parametrize("custodian", [False, True])
async def test_s9_role_matrix_no_leak(fa, role, custodian):
    from celerp.services.permissions import role_has_permission

    van, _ = await _registered_van(fa)
    await _tag(fa, van, "VAN-1")
    uid, headers = await fa.member(role)
    if custodian:
        await _move(fa, van, date="2026-02-02", custodian={"user_id": str(uid)})
    settings = {}
    views = role_has_permission(settings, role, "view_fixed_assets")
    money = role_has_permission(settings, role, "view_financial_reports")

    detail = await fa.client.get(f"/fixed-assets/api/assets/{van}", headers=headers)
    files = await fa.client.get(f"/fixed-assets/api/assets/{van}/files", headers=headers)
    if views:
        assert detail.status_code == 200 and (detail.json()["cost"] is not None) == money
        assert (len(files.json()["linked"]) == 1) == money
    else:
        assert detail.status_code == 403 and files.status_code == 403

    found = (await fa.client.get("/search", headers=headers, params={"q": "VAN-1"})).json()["results"]
    rows = (found.get("celerp-fixed-assets") or {}).get("items") or []
    assert [r["id"] for r in rows] == ([van] if views else [])
    assert all(set(r) == {"id", "label", "href", "subtitle"} for r in rows)

    schedule = await fa.client.get("/fixed-assets/api/reports/schedule", headers=headers,
                                   params={"date_from": "2026-01-01", "date_to": "2026-03-31"})
    assert (schedule.status_code == 200) == money
    exported = await _ui(fa, "GET", "/fixed-assets/reports/register.csv", headers)
    assert ("32000" in exported.text) == money

    mine = await fa.ok(await fa.client.get("/fixed-assets/api/mine", headers=headers))
    assert [a["id"] for a in mine["items"]] == ([van] if custodian else [])
    assert all("cost" not in a and "sources" not in a for a in mine["items"])
    mine_one = await fa.client.get(f"/fixed-assets/api/mine/{van}", headers=headers)
    assert mine_one.status_code == (200 if custodian else 404)
    if custodian:
        assert "files" in mine_one.json() and all(f["document_tag"] in ("photos", "manuals")
                                                  for f in mine_one.json()["files"])
