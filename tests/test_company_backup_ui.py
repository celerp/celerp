# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Company backup pages (UI side), driven against the real API on committed test data:
download from Settings, restore from Settings, from the add-company chooser and on a
fresh installation, the first-run chooser and the migration completion page."""

from __future__ import annotations

import base64
import html as _html
import io
import json
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from company_backup_support import company, download, manifest, members, owner, read, rezip, token
from migration_support import code_config, count, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

UPLOAD_COOKIE = "celerp_company_backup_upload"
CONTENTS = ("Contains this company's records, settings and attachments. Users, passwords, "
            "connections and share links stay with this Celerp installation.")
SEPARATE = ("Celerp restores this as a separate company so your current company is not "
            "overwritten. You can verify the restored company before deactivating the old one.")
NOTHING_WRITTEN = "Nothing has been written yet."
RESTORED = "Restored from the backup of "
CHOICES = ["Start a new company", "Move from another system", "Restore a company backup", "Try sample company"]
RECOVER = "Recover an entire Celerp installation"
REPO = Path(__file__).resolve().parents[1]


def _factory(transport):
    def make(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url="http://api", headers=merged, transport=transport,
                                 follow_redirects=follow_redirects, timeout=timeout)
    return make


@pytest_asyncio.fixture
async def ui(real_client, tmp_path, monkeypatch):
    """The UI app, its API calls routed to the real API app on `real_engine`."""
    import celerp.main
    import ui.api_client as api
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(api, "_local_client", _factory(httpx.ASGITransport(app=celerp.main.app)))
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://localhost", follow_redirects=False) as c:
        yield c


class Router(httpx.AsyncBaseTransport):
    """Answers the overridden API routes in the test and sends everything else to the real API."""

    def __init__(self) -> None:
        import celerp.main
        self.overrides: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []
        self._real = httpx.ASGITransport(app=celerp.main.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        hook = self.overrides.get((request.method, request.url.path))
        if hook is not None:
            return hook(request)
        return await self._real.handle_async_request(request)


@pytest_asyncio.fixture
async def routed_ui(real_client, tmp_path, monkeypatch):
    """The UI app with a Router in front of the real API; yields (client, router)."""
    import ui.api_client as api
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    router = Router()
    monkeypatch.setattr(api, "_local_client", _factory(router))
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://localhost", follow_redirects=False) as c:
        yield c, router


def _page(r: httpx.Response) -> str:
    return re.sub(r"<script\b.*?</script>", "", _html.unescape(r.text), flags=re.S | re.I)


def _link(page: str, href: str, label: str) -> bool:
    return re.search(rf'<a\b[^>]*href="{re.escape(href)}"[^>]*>[^<]*{re.escape(label)}[^<]*</a>', page) is not None


def _anchors(page: str) -> list[tuple[str, str, str]]:
    """(href, class, visible text) of every link on the page."""
    out = []
    for attrs, inner in re.findall(r"<a\b([^>]*)>(.*?)</a>", page, flags=re.S):
        href = re.search(r'href="([^"]*)"', attrs)
        cls = re.search(r'class="([^"]*)"', attrs)
        out.append((href.group(1) if href else "", cls.group(1) if cls else "",
                    re.sub(r"<[^>]+>", "", inner).strip()))
    return out


def _cards(page: str) -> list[str]:
    """Visible labels of the chooser cards, links and buttons alike, in page order."""
    found = re.findall(r'<(a|button)\b[^>]*class="[^"]*\bquick-link-action\b[^"]*"[^>]*>(.*?)</\1>', page, flags=re.S)
    return [re.sub(r"<[^>]+>", "", inner).strip() for _, inner in found]


def _cookie(r: httpx.Response, name: str) -> str:
    header = next(c for c in r.headers.get_list("set-cookie") if c.startswith(f"{name}="))
    return header.split(";", 1)[0].split("=", 1)[1].strip('"')


def _hidden(page: str) -> dict:
    fields = {}
    for tag in re.findall(r'<input\b[^>]*type="hidden"[^>]*>', page):
        name = re.search(r'name="([^"]*)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = value.group(1) if value else ""
    return fields


def _claims(tok: str) -> dict:
    body = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


async def _company_ids(engine) -> set[str]:
    async with engine.connect() as conn:
        return {str(i) for i in (await conn.execute(text("SELECT id FROM companies"))).scalars().all()}


async def _setup(engine, name: str = "Alpha Trading", marker: str = "alpha-marker"):
    """The installation owner with one company; returns (user_id, company_id, token)."""
    user = await owner(engine)
    cid = await company(engine, user, name, marker)
    return user, cid, await token(engine, user, cid)


def _dates(iso: str) -> set[str]:
    """Ways a page may print the calendar date of an ISO timestamp."""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return {d.strftime("%Y-%m-%d"), d.strftime("%d %b %Y"), f"{d.day} {d.strftime('%b %Y')}",
            d.strftime("%d %B %Y"), f"{d.day} {d.strftime('%B %Y')}", d.strftime("%b %d, %Y"),
            f"{d.strftime('%b')} {d.day}, {d.year}", d.strftime("%B %d, %Y"), f"{d.strftime('%B')} {d.day}, {d.year}"}


def _names_date(page: str, iso: str) -> bool:
    m = re.search(re.escape(RESTORED) + r"([^<]*?)\.", page)
    return m is not None and m.group(1).strip() in _dates(iso)


async def _follow(ui: httpx.AsyncClient, r: httpx.Response, hops: int = 5) -> httpx.Response:
    """Follow UI redirects, carrying the session cookie each response sets."""
    for _ in range(hops):
        if r.status_code not in (301, 302, 303, 307):
            return r
        if any(c.startswith("celerp_token=") for c in r.headers.get_list("set-cookie")):
            ui.cookies.set("celerp_token", _cookie(r, "celerp_token"))
        r = await ui.get(r.headers["location"])
    return r


async def _upload_and_preview(ui, base: str, data: bytes, **form) -> httpx.Response:
    r = await ui.post(f"{base}/read", files={"file": ("alpha.celerp-company", data, "application/octet-stream")},
                      data=form or None)
    assert r.status_code == 200, r.text
    ui.cookies.set(UPLOAD_COOKIE, _cookie(r, UPLOAD_COOKIE), path=base)
    return r


# ── Settings > Backup ────────────────────────────────────────────────────────

async def test_settings_backup_tab_one_click_download(ui, real_engine):
    """The Backup tab's download link returns the company backup file itself."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    r = await ui.get("/settings/general?tab=backup")
    assert r.status_code == 200, r.text
    assert _link(_page(r), "/company-backup/download", "Download company backup")

    r = await ui.get("/company-backup/download")
    assert r.status_code == 200, r.text
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition and ".celerp-company" in disposition
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        assert json.loads(zf.read("manifest.json"))["company"]["name"] == "Alpha Trading"


async def test_settings_backup_tab_states_contents_once(ui, real_engine):
    """The Backup tab says once what a company backup contains and offers restore."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    page = _page(await ui.get("/settings/general?tab=backup"))
    assert page.count(CONTENTS) == 1
    assert _link(page, "/settings/restore-backup", "Restore company backup")


async def test_settings_restore_explains_and_notices_backup_date(ui, real_engine, real_client):
    """Settings restore previews with the separate-company explanation, then opens the restored company with the backup date."""
    _, alpha, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    created_at = manifest(data)["created_at"]
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    r = await ui.get(base)
    assert r.status_code == 200, r.text
    assert 'type="file"' in r.text and f'action="{base}/read"' in r.text

    before = await _company_ids(real_engine)
    r = await _upload_and_preview(ui, base, data)
    preview = _page(r)
    for s in ("Alpha Trading", NOTHING_WRITTEN, SEPARATE):
        assert s in preview, s
    assert await _company_ids(real_engine) == before

    r = await ui.post(f"{base}/restore", data=_hidden(preview))
    assert r.status_code == 303, r.text
    added = await _company_ids(real_engine) - before
    assert len(added) == 1
    restored = added.pop()
    assert _claims(_cookie(r, "celerp_token"))["company_id"] == restored != str(alpha)

    page = _page(await _follow(ui, r))
    assert _names_date(page, created_at), page[:2000]


# ── Add-company chooser ──────────────────────────────────────────────────────

async def test_add_company_chooser_restore_from_backup(ui, real_engine):
    """The add-company chooser offers Restore from backup and no company copy."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    r = await ui.get("/setup/new-company")
    assert r.status_code == 200, r.text
    page = _page(r)
    base = "/setup/new-company/restore-backup"
    assert _link(page, base, "Restore from backup")
    assert "/setup/new-company/open-copy" not in page
    assert re.search(r"company copy", page, re.I) is None

    r = await ui.get(base)
    assert r.status_code == 200, r.text
    assert 'type="file"' in r.text and f'action="{base}/read"' in r.text


async def test_add_company_restore_previews_before_write(ui, real_engine, real_client):
    """Restoring from the add-company chooser previews without writing, then creates one company."""
    _, alpha, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/setup/new-company/restore-backup"

    before = await _company_ids(real_engine)
    ledger, projections = await count(real_engine, "ledger"), await count(real_engine, "projections")
    r = await _upload_and_preview(ui, base, data)
    preview = _page(r)
    assert "Alpha Trading" in preview and NOTHING_WRITTEN in preview
    assert await _company_ids(real_engine) == before
    assert await count(real_engine, "ledger") == ledger
    assert await count(real_engine, "projections") == projections

    r = await ui.post(f"{base}/restore", data=_hidden(preview))
    assert r.status_code == 303, r.text
    added = await _company_ids(real_engine) - before
    assert len(added) == 1
    assert _claims(_cookie(r, "celerp_token"))["company_id"] == added.pop() != str(alpha)


# ── Fresh installation ───────────────────────────────────────────────────────

async def test_bootstrap_restore_ui_journey(ui, real_engine, real_client, code_config):
    """A fresh installation restores a company backup with the setup code and a new owner account, then enters it."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    created_at = manifest(data)["created_at"]
    summary = await read(real_client, tok, data)
    assert summary.status_code == 200, summary.text
    records = summary.json()["records"]
    async with real_engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))
    ui.cookies.clear()

    r = await ui.get("/setup")
    assert r.status_code == 200, r.text
    base = "/setup/restore-backup"
    assert _link(_page(r), base, "Restore a company backup")
    r = await ui.get(base)
    assert r.status_code == 200, r.text
    assert 'name="setup_code"' in r.text and 'type="file"' in r.text

    r = await _upload_and_preview(ui, base, data, setup_code=code_config)
    preview = _page(r)
    assert "Alpha Trading" in preview and NOTHING_WRITTEN in preview
    assert any(d in preview for d in _dates(created_at))
    assert re.search(rf"\b{records}\b", preview)
    for field in ("name", "email", "password", "setup_code"):
        assert f'name="{field}"' in preview, field
    assert await count(real_engine, "companies") == 0 and await count(real_engine, "users") == 0

    account = {"name": "Owner", "email": "owner@example.com", "password": "ownerpw123",
               "confirm_password": "ownerpw123", "setup_code": code_config}
    r = await ui.post(f"{base}/restore", data={**_hidden(preview), **account})
    assert r.status_code == 303, r.text
    assert await count(real_engine, "companies") == 1
    assert await count(real_engine, "users") == 1
    assert _cookie(r, "celerp_token")
    page = _page(await _follow(ui, r))
    assert "Alpha Trading" in page
    assert _names_date(page, created_at), page[:2000]


# ── Old company copy pages ───────────────────────────────────────────────────

async def test_company_copy_page_gone(ui, real_engine):
    """The company copy pages no longer exist."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    for path in ("/company-copy", "/company-copy/abc/download", "/setup/open-copy", "/setup/new-company/open-copy"):
        assert (await ui.get(path)).status_code == 404, path


# ── First-run chooser ────────────────────────────────────────────────────────

async def test_first_run_chooser_choices(ui, real_engine):
    """The first-run chooser offers exactly the four ways to start and a small whole-installation link."""
    r = await ui.get("/setup")
    assert r.status_code == 200, r.text
    page = _page(r)
    assert _cards(page) == CHOICES
    anchors = _anchors(page)
    assert ("/setup/restore-backup", "Restore a company backup") in [(h, t) for h, c, t in anchors
                                                                      if "quick-link-action" in c]
    recover = [(h, c) for h, c, t in anchors if t == RECOVER]
    assert recover == [(h, c) for h, c in recover if h == "/setup/import-backup" and "quick-link-action" not in c]
    assert len(recover) == 1


async def test_first_run_chooser_no_competing_copy_and_backup(ui, real_engine):
    """The first-run chooser has no company copy choice and no card for the whole-installation restore."""
    page = _page(await ui.get("/setup"))
    assert re.search(r"\bcop(y|ies)\b", page, re.I) is None
    assert "Restore a Celerp backup" not in page
    assert "/setup/open-copy" not in page
    cards = [h for h, c, _ in _anchors(page) if "quick-link-action" in c]
    assert "/setup/import-backup" not in cards
    assert [h for h, _, _ in _anchors(page)].count("/setup/import-backup") == 1


# ── Migration completion ─────────────────────────────────────────────────────

async def test_migration_complete_download_uses_company_exporter(routed_ui, real_engine):
    """The migration completion page downloads the migrated company's backup through the company backup API."""
    ui, router = routed_ui
    _, cid, tok = await _setup(real_engine)
    run_id = str(uuid.uuid4())
    run = {"id": run_id, "company_id": str(cid), "company_name": "Harbor Goods Ltd",
           "source_system": "manager_io", "mode": "full_history", "cutover_date": None,
           "status": "completed", "current_phase": "ready_to_finalize", "phases": [], "coverage": [],
           "error_summary": {}, "retention_until": None, "source_deleted": False,
           "prepared_by": "Example Bookkeeping", "is_bootstrap_run": False, "is_sample": False}
    body = b"PK\x05\x06" + b"\x00" * 18
    router.overrides[("GET", f"/migrations/{run_id}")] = lambda req: httpx.Response(200, json=run)
    router.overrides[("GET", f"/migrations/{run_id}/reconciliation")] = \
        lambda req: httpx.Response(200, json={"generated_at": None, "blockers": 0, "rows": []})
    router.overrides[("GET", "/company-backups/download")] = lambda req: httpx.Response(
        200, content=body, headers={"content-type": "application/zip",
                                    "content-disposition": 'attachment; filename="harbor.celerp-company"'})
    ui.cookies.set("celerp_token", tok)

    r = await ui.get(f"/migrations/{run_id}/complete")
    assert r.status_code == 200, r.text
    page = _page(r)
    href = f"/company-backup/download?from_run={run_id}"
    assert _link(page, href, "Download company backup")
    assert "/company-copy" not in page

    r = await ui.get(href)
    assert r.status_code == 200, r.text
    assert r.content == body
    assert ".celerp-company" in r.headers.get("content-disposition", "")
    calls = [q for q in router.requests if q.url.path == "/company-backups/download"]
    assert len(calls) == 1 and calls[0].method == "GET"
    assert dict(calls[0].url.params) == {"run_id": run_id}


async def test_migration_has_no_own_backup_implementation():
    """The migration code carries no backup implementation of its own and reaches backups only through the company backup API."""
    ui_src = (REPO / "ui" / "routes" / "migrations.py").read_text()
    api_src = (REPO / "celerp" / "routers" / "migrations.py").read_text()
    for src in (ui_src, api_src):
        for banned in ("zipfile", "tarfile", "company_copy", "/company-copy", "export_company",
                       "backup_export", "export_full"):
            assert banned not in src, banned
    assert set(re.findall(r"""["'](/[\w/-]*backup[\w/-]*)""", ui_src)) == {"/company-backup/download"}
    assert re.search(r"backup", api_src, re.I) is None


# ── Missing module ───────────────────────────────────────────────────────────

async def test_restore_page_names_missing_module(ui, real_engine, real_client):
    """The restore page names the module a backup needs and restores nothing."""
    _, _, tok = await _setup(real_engine)
    parts = members(await download(real_client, tok))
    meta = json.loads(parts["manifest.json"])
    meta["modules"]["enabled"] = list(meta["modules"]["enabled"]) + ["celerp-example-widgets"]
    meta["modules"].setdefault("versions", {})["celerp-example-widgets"] = "1.0.0"
    parts["manifest.json"] = json.dumps(meta).encode()
    data = rezip(parts)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    before = await _company_ids(real_engine)
    r = await ui.post(f"{base}/read", files={"file": ("alpha.celerp-company", data, "application/octet-stream")})
    page = _page(r)
    assert "needs the celerp-example-widgets module" in page
    assert "Nothing was restored." in page
    assert 'type="file"' in r.text
    assert await _company_ids(real_engine) == before


# ── Separation from System Recovery ──────────────────────────────────────────

async def test_system_recovery_separate_from_company_backup(ui, real_engine):
    """Install owners reach System Recovery from the Backup tab, and it is a separate page from the company backup."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    tab = _page(await ui.get("/settings/general?tab=backup"))
    assert _link(tab, "/settings/system-recovery", "System Recovery")
    r = await ui.get("/settings/system-recovery")
    assert r.status_code == 200, r.text
    recovery = _page(r)
    assert "Replaces the Celerp database and installation files, affecting every company and user." in recovery
    assert CONTENTS not in recovery
    assert "/company-backup/download" not in recovery and "/settings/restore-backup" not in recovery
