# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Move several companies: several source files in one upload, each its own company
through the same steps. A list shows each file's step, newest first, and each company
is reviewed, started and finished on its own; nothing starts on the user's behalf."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from company_backup_support import company, member, owner, token
from migration_support import auth, load_run, maker, multipart, real_client, real_engine, scan_upload  # noqa: F401
from test_migration_firm_template import runner  # noqa: F401
from ui.i18n import t

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures" / "manager_io"
BASE = "/migrations/several"
NOT_A_BACKUP = ("notes.txt", b"These are not the books of any company.\n")


def _manager(name: str) -> tuple[str, bytes]:
    return f"{name}.manager", (FIXTURES / f"{name}.manager").read_bytes()


async def _owner_headers(engine, *, with_company: bool = False):
    user = await owner(engine)
    firm = await company(engine, user, "Firm books", "several-firm")
    headers = auth(await token(engine, user, firm))
    return (headers, firm) if with_company else headers


async def _upload(client, headers, *files, **data):
    return await client.post(BASE, headers=headers, files=multipart(*files), data=data or None)


async def _rows(client, headers) -> list[dict]:
    r = await client.get(BASE, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def _companies(engine) -> int:
    from celerp.models.company import Company
    async with maker(engine)() as s:
        return await s.scalar(select(func.count()).select_from(Company))


async def _start(client, headers, row_id: str, company_name: str) -> str:
    """Open one file of the list and take it through the wizard's own steps to its start."""
    r = await client.post(f"{BASE}/{row_id}/open", headers=headers)
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    r = await client.post("/migrations/scan/read", headers=headers, json={"scan_token": scan_token})
    assert r.status_code == 200 and "scan" in r.json(), r.text
    r = await client.post("/migrations/scan/decisions", headers=headers, json={
        "scan_token": scan_token, "mode": "full_history", "cutover_date": None, "mappings": {},
        "prepared_by": None})
    assert r.status_code == 200, r.text
    r = await client.post("/migrations/start-from-scan", headers=headers,
                          json={"scan_token": scan_token, "company_name": company_name})
    assert r.status_code == 201, r.text
    return r.json()["run_id"]


async def test_bulk_migration(real_client, real_engine, runner):
    """Several files in one upload: each is its own scan, listed newest first with its
    step; nothing is created until one is started, and each starts and runs on its own."""
    from celerp.services import migrations

    headers = await _owner_headers(real_engine)
    assert await _rows(real_client, headers) == []
    before = await _companies(real_engine)

    r = await _upload(real_client, headers, _manager("basic"), _manager("orders"), NOT_A_BACKUP,
                      prepared_by="Example Accounting")
    assert r.status_code == 201, r.text
    assert await _companies(real_engine) == before  # an upload creates no company

    rows = await _rows(real_client, headers)
    assert [row["file_name"] for row in rows] == ["notes.txt", "orders.manager", "basic.manager"]  # newest first
    assert [row["state"] for row in rows] == ["unreadable", "ready", "ready"]
    assert rows[0]["error"] and rows[0]["run_id"] is None
    assert {row["source_system"] for row in rows[1:]} == {"manager_io"}
    assert all(row["uploaded_at"] for row in rows)

    orders, basic = rows[1]["id"], rows[2]["id"]
    run_id = await _start(real_client, headers, basic, "Basic Client")
    by_id = {row["id"]: row for row in await _rows(real_client, headers)}
    assert by_id[basic]["state"] == "started" and by_id[basic]["run_id"] == run_id
    assert by_id[basic]["company_name"] == "Basic Client"
    assert by_id[orders]["state"] == "ready" and by_id[orders]["run_id"] is None  # never started for the user

    # The started file opens to its run; its company goes through the run on its own.
    r = await real_client.post(f"{BASE}/{basic}/open", headers=headers)
    assert r.status_code == 200 and r.json() == {"run_id": run_id}, r.text
    await migrations.run_migration(uuid.UUID(run_id))
    second = await _start(real_client, headers, orders, "Orders Client")
    await migrations.run_migration(uuid.UUID(second))
    by_id = {row["id"]: row for row in await _rows(real_client, headers)}
    assert by_id[basic]["run_status"] == by_id[orders]["run_status"] == "ready_to_finalize"
    for rid in (run_id, second):
        assert (await load_run(real_engine, uuid.UUID(rid))).status == "ready_to_finalize"  # finished by nobody
    assert await _companies(real_engine) == before + 2

    r = await real_client.post(f"{BASE}/{rows[0]['id']}/open", headers=headers)
    assert r.status_code == 422 and r.json()["detail"] == rows[0]["error"], r.text


async def test_bulk_migration_keeps_every_file(real_client, real_engine):
    """A second upload and an ordinary one-company upload each leave the earlier files
    in the list ready to continue."""
    headers = await _owner_headers(real_engine)
    assert (await _upload(real_client, headers, _manager("basic"))).status_code == 201
    assert (await _upload(real_client, headers, _manager("stock"))).status_code == 201
    r = await scan_upload(real_client, _manager("orders")[1], token=headers["Authorization"].split()[1],
                          name="orders.manager")
    assert r.status_code == 200, r.text
    rows = await _rows(real_client, headers)
    assert [row["file_name"] for row in rows] == ["stock.manager", "basic.manager"]
    for row in rows:
        r = await real_client.post(f"{BASE}/{row['id']}/open", headers=headers)
        assert r.status_code == 200 and r.json()["scan_token"], r.text


async def test_bulk_migration_refuses_what_is_not_one_company(real_client, real_engine):
    """A file that is one part of an export set is named as such and points to moving
    that client on its own; an upload with no file, or more files than one upload takes,
    is refused with the reason and lists nothing."""
    from celerp.services import migration_scan_store as store
    from fixtures.xero_export import build

    headers = await _owner_headers(real_engine)
    name, data = next((n, d) for n, d in build.files().items() if n.endswith(".csv"))
    assert (await _upload(real_client, headers, (name, data))).status_code == 201
    [row] = await _rows(real_client, headers)
    assert row["state"] == "unreadable"
    assert row["error"] == t("migration.several_export_set", source="Xero"), row

    r = await real_client.post(BASE, headers=headers, data={"prepared_by": "x"})
    assert r.status_code == 422 and r.json()["detail"] == t("migration.choose_file"), r.text
    many = [(f"c{i}.manager", _manager("basic")[1]) for i in range(store.MAX_ARTIFACTS + 1)]
    r = await _upload(real_client, headers, *many)
    assert r.status_code == 422 and r.json()["detail"] == t(
        "migration.several_too_many", count=store.MAX_ARTIFACTS), r.text
    assert len(await _rows(real_client, headers)) == 1


async def test_bulk_migration_remove_and_expiry(real_client, real_engine, monkeypatch):
    """Remove deletes a file and its upload; a file left past its time is listed as gone
    with the way forward, and opening it says so."""
    from celerp.services import migration_scan_store as store

    headers = await _owner_headers(real_engine)
    await _upload(real_client, headers, _manager("basic"), _manager("orders"))
    orders, basic = [row["id"] for row in await _rows(real_client, headers)]
    r = await real_client.post(f"{BASE}/{orders}/open", headers=headers)
    orders_token = r.json()["scan_token"]

    r = await real_client.delete(f"{BASE}/{orders}", headers=headers)
    assert r.status_code == 204, r.text
    assert [row["id"] for row in await _rows(real_client, headers)] == [basic]
    with pytest.raises(store.ScanStoreError):
        store.scan_owner(orders_token)  # the uploaded file is gone from this server
    assert (await real_client.delete(f"{BASE}/{orders}", headers=headers)).status_code == 404

    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now + store.SEVERAL_TTL_SECONDS + 1)
    [row] = await _rows(real_client, headers)
    assert row["state"] == "gone" and row["run_id"] is None
    r = await real_client.post(f"{BASE}/{basic}/open", headers=headers)
    assert r.status_code == 410 and r.json()["detail"] == t("migration.several_gone"), r.text


async def test_bulk_migration_lasts_longer_than_one_upload(real_client, real_engine, monkeypatch):
    """A file of several waits its turn past the hour a one-company upload is kept."""
    from celerp.services import migration_scan_store as store

    headers = await _owner_headers(real_engine)
    await _upload(real_client, headers, _manager("basic"))
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now + store.SCAN_TTL_SECONDS + 60)
    [row] = await _rows(real_client, headers)
    assert row["state"] == "ready"
    assert (await real_client.post(f"{BASE}/{row['id']}/open", headers=headers)).status_code == 200


async def test_bulk_migration_owner_only(real_client, real_engine):
    """Only the company owner moves companies in, and each person's files are their own."""
    headers, firm = await _owner_headers(real_engine, with_company=True)
    await _upload(real_client, headers, _manager("basic"))
    [row] = await _rows(real_client, headers)

    staff = await owner(real_engine, "staff@example.com", "Staff")
    await member(real_engine, staff, firm, "admin")
    admin = auth(await token(real_engine, staff, firm, "admin"))
    for method, path in (("get", BASE), ("post", BASE), ("post", f"{BASE}/{row['id']}/open"),
                         ("delete", f"{BASE}/{row['id']}")):
        r = await getattr(real_client, method)(path, headers=admin)
        assert r.status_code == 403, (method, path, r.text)
    assert (await real_client.get(BASE)).status_code == 401

    other = await owner(real_engine, "other@example.com", "Other")
    theirs = auth(await token(real_engine, other, await company(real_engine, other, "Other books", "several-other")))
    assert await _rows(real_client, theirs) == []
    assert (await real_client.post(f"{BASE}/{row['id']}/open", headers=theirs)).status_code == 404
    assert (await real_client.delete(f"{BASE}/{row['id']}", headers=theirs)).status_code == 404
    r = await real_client.post(f"{BASE}/{row['id']}/open", headers=headers)
    scan_token = r.json()["scan_token"]
    r = await real_client.post("/migrations/scan/read", headers=theirs, json={"scan_token": scan_token})
    assert r.status_code == 410, r.text  # another person's upload is not theirs to read


# ── The page ─────────────────────────────────────────────────────────────────

ROWS = [
    {"id": "r3", "file_name": "notes.txt", "uploaded_at": "2026-10-17T03:00:02+00:00", "state": "unreadable",
     "error": "This file was not recognised.", "source_system": None, "run_id": None, "run_status": None,
     "company_name": None},
    {"id": "r2", "file_name": "orders.manager", "uploaded_at": "2026-10-17T03:00:01+00:00", "state": "started",
     "error": None, "source_system": "manager_io", "run_id": "run-2", "run_status": "ready_to_finalize",
     "company_name": "Orders Client"},
    {"id": "r1", "file_name": "basic.manager", "uploaded_at": "2026-10-17T03:00:00+00:00", "state": "ready",
     "error": None, "source_system": "manager_io", "run_id": None, "run_status": None, "company_name": None},
    {"id": "r0", "file_name": "old.manager", "uploaded_at": "2026-10-16T03:00:00+00:00", "state": "gone",
     "error": None, "source_system": "manager_io", "run_id": None, "run_status": None, "company_name": None},
]


@pytest.fixture
def page_api(monkeypatch):
    import ui.api_client as api
    calls: list = []

    async def several(token):
        return {"items": ROWS}

    async def open_(token, row_id):
        calls.append(("open", row_id))
        return {"scan_token": "tok-" + row_id, "prepared_by": "Example Accounting"}

    async def remove(token, row_id):
        calls.append(("remove", row_id))

    async def sources():
        return [{"key": "manager_io", "display_name": "Manager.io", "accepts": "", "artifacts": [
            {"key": "backup", "label": "Manager backup", "extensions": [".manager"]}], "checklist": None},
            {"key": "xero", "display_name": "Xero", "accepts": "", "artifacts": [
                {"key": "a", "label": "a", "extensions": [".csv"]}, {"key": "b", "label": "b", "extensions": [".xlsx"]}],
             "checklist": {"source": "Xero"}}]

    monkeypatch.setattr(api, "migration_several", several, raising=False)
    monkeypatch.setattr(api, "migration_several_open", open_, raising=False)
    monkeypatch.setattr(api, "migration_several_remove", remove, raising=False)
    monkeypatch.setattr(api, "migration_sources", sources)
    return calls


async def _ui(method: str, path: str, **kwargs):
    from test_import_navigation import _ui_request
    return await _ui_request(method, path, follow_redirects=False, **kwargs)


async def test_bulk_migration_page_lists_each_step(page_api):
    """The page takes several files of a one-file source and lists each file with its
    step, newest first, the next action on each row and why a row cannot continue."""
    r = await _ui("GET", "/setup/new-company/migrate/several")
    assert r.status_code == 200, r.text
    html = r.text
    assert t("migration.several_title") in html and t("migration.several_hint") in html
    assert 'multiple' in html and 'name="files"' in html
    assert 'value="manager_io"' in html and 'value="xero"' not in html  # export sets move one at a time
    order = [html.index(row["file_name"]) for row in ROWS]
    assert order == sorted(order)
    assert t("migration.several_state.ready") in html and t("migration.several_state.gone") in html
    assert t("migration.several_state.unreadable") in html and "This file was not recognised." in html
    assert t("migration.status.ready_to_finalize") in html and "Orders Client" in html
    assert 'href="/migrations/run-2"' in html
    assert 'action="/setup/new-company/migrate/several/r1/open"' in html
    assert t("migration.several_gone") in html
    assert html.count('hx-post="/setup/new-company/migrate/several/') == 4  # every row can be removed
    assert html.count("cell--number") == 0 and ">--<" in html  # no company yet shows --


async def test_bulk_migration_continue_opens_the_wizard(page_api):
    """Continue carries that file's upload into the one-company wizard at its first step
    after the upload, with the Prepared by name typed for the whole upload."""
    import ui.routes.migrations as ui

    resp = await _ui("POST", "/setup/new-company/migrate/several/r1/open")
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/new-company/migrate/coverage"
    cookies = resp.headers.get_list("set-cookie")
    assert any(c.startswith(f"{ui.SCAN_COOKIE}=tok-r1") for c in cookies), cookies
    assert any(c.startswith(f"{ui.PREPARED_BY_COOKIE}=") and "Example" in c for c in cookies), cookies
    assert page_api == [("open", "r1")]

    r = await _ui("POST", "/setup/new-company/migrate/several/r1/remove")
    html = r.text
    assert r.status_code == 200 and page_api[-1] == ("remove", "r1") and 'id="several-list"' in html


async def test_bulk_migration_chooser_offers_it():
    """Adding a company offers moving several at once beside moving one."""
    r = await _ui("GET", "/setup/new-company")
    assert r.status_code == 200, r.text
    assert 'href="/setup/new-company/migrate/several"' in r.text
    assert t("setup.card_move_several") in r.text and t("setup.card_move_several_desc") in r.text
