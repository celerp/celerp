# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Company backup pages (UI side), driven against the real API on committed test data:
download from Settings, restore from Settings, from the add-company chooser and on a
fresh installation, the first-run chooser and the migration completion page."""

from __future__ import annotations

import base64
import html as _html
import inspect
import io
import json
import re
import secrets
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
from ui.components.table import format_value
from ui.i18n import t

UPLOAD_COOKIE = "celerp_company_backup_upload"
CONTENTS = ("Contains this company's records, settings and attachments. Users, passwords, "
            "connections and share links stay with this Celerp installation.")
SEPARATE = ("Celerp restores this as a separate company so your current company is not "
            "overwritten. You can verify the restored company before deactivating the old one.")
NOTHING_WRITTEN = "Nothing has been written yet."
RESTORED = "Restored from the company copy made on "
CLIENT_EMAIL = "client@example.com"
SENT_TO_CLIENT = f"Sent to {CLIENT_EMAIL}."
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
            answer = hook(request)
            return await answer if inspect.isawaitable(answer) else answer
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
    assert _link(_page(r), "/company-backup/download", "Download company copy")

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
    assert _link(page, "/settings/restore-backup", "Open a company copy")


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
    """The add-company chooser offers Open a company copy through the one restore wizard."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    r = await ui.get("/setup/new-company")
    assert r.status_code == 200, r.text
    page = _page(r)
    base = "/setup/new-company/restore-backup"
    assert _link(page, base, "Open a company copy")
    assert "/setup/new-company/open-copy" not in page

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
    assert _link(_page(r), base, "Open a company copy")
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


# ── Setup form start options ─────────────────────────────────────────────────

async def test_setup_form_offers_one_restore_entry_point(ui, real_engine):
    """The setup form links Open a company copy once; the whole-installation recovery
    sits on the restore page, so restore has one entry point."""
    page = _page(await ui.get("/setup"))
    assert "Open a company copy" in page
    assert "/setup/open-copy" not in page
    hrefs = [h for h, _, _ in _anchors(page)]
    assert hrefs.count("/setup/restore-backup") == 1
    assert "/setup/import-backup" not in hrefs
    restore = _page(await ui.get("/setup/restore-backup"))
    assert [(h, t) for h, _, t in _anchors(restore) if h == "/setup/import-backup"] == [("/setup/import-backup", RECOVER)]


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
    assert _link(page, href, "Download company copy")
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
    """The preview names the module a backup needs, offers to import it, and offers no restore."""
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
    assert r.status_code == 200, r.text
    page = _page(r)
    assert "celerp-example-widgets" in page
    assert "Not installed here. Import the module file to continue." in page
    assert f'action="{base}/import-module"' in r.text and 'name="module"' in r.text
    assert f'action="{base}/restore"' not in r.text
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


TEAM_BEFORE = "Team members who keep their access and roles in the restored company: 2"
TEAM_AFTER = "Team members given access to this company with their current roles: 2"


async def test_settings_restore_states_team_access_before_and_after(ui, real_engine, real_client):
    """A same-company Settings restore says on the preview and on the restored company how many team members get access."""
    from company_backup_support import member
    _, alpha, tok = await _setup(real_engine)
    await member(real_engine, await owner(real_engine, "clerk@example.com", "Clerk"), alpha, "viewer")
    await member(real_engine, await owner(real_engine, "buyer@example.com", "Buyer"), alpha, "manager")
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    preview = _page(await _upload_and_preview(ui, base, data))
    assert TEAM_BEFORE in preview
    r = await ui.post(f"{base}/restore", data=_hidden(preview))
    assert r.status_code == 303, r.text
    page = _page(await _follow(ui, r))
    assert TEAM_AFTER in page, page[:2000]
    assert "company_backup." not in page



# ── Destination, scope, cancel and outcome ───────────────────────────────────

async def _names(engine) -> list[str]:
    async with engine.connect() as conn:
        return sorted((await conn.execute(text("SELECT name FROM companies"))).scalars().all())


def _stages(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "company_backups" / "uploads"
    return sorted(p for p in folder.iterdir() if p.suffix == ".upload") if folder.is_dir() else []


async def test_restore_preview_names_destination_and_states_scope(ui, real_engine, real_client):
    """The preview proposes a name that is not one of the owner's companies, editable, and
    says what the backup brings and what stays behind; the chosen name is the new company's."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    r = await _upload_and_preview(ui, base, data)
    page = _page(r)
    assert re.search(r'<input\b[^>]*name="company_name"[^>]*value="Alpha Trading \(Restored\)"', page), page[:3000]
    # What comes and what stays behind read as two short lists.
    lists = {m[1]: re.findall(r"<li>([^<]+)</li>", m[2])
             for m in re.finditer(r"<strong>([^<]+)</strong>\s*<ul[^>]*>(.*?)</ul>", page, re.S)}
    assert lists["Included"] == ["records", "settings", "attached files"]
    assert lists["Not included"] == ["users", "passwords", "sign-in sessions", "connections", "share links"]
    assert "These stay with the installation the company copy was made on." in page

    r = await ui.post(f"{base}/restore", data={**_hidden(page), "company_name": "Alpha Trading"})
    page = _page(r)
    assert r.status_code == 200, r.text
    assert t("company_backup.err_name_taken") in page, page[:3000]
    assert re.search(r'name="company_name"[^>]*value="Alpha Trading"', page)
    assert await _names(real_engine) == ["Alpha Trading"]

    r = await ui.post(f"{base}/restore", data={**_hidden(page), "company_name": "Alpha Archive"})
    assert r.status_code == 303, r.text
    assert await _names(real_engine) == ["Alpha Archive", "Alpha Trading"]


async def test_choose_other_file_deletes_upload(ui, real_engine, real_client, tmp_path):
    """Choosing another file deletes the upload and goes back to choosing a file."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    page = _page(await _upload_and_preview(ui, base, data))
    assert f'action="{base}/discard"' in page
    assert len(_stages(tmp_path)) == 1
    r = await ui.post(f"{base}/discard")
    assert r.status_code == 303 and r.headers["location"] == base
    assert _stages(tmp_path) == []
    assert any(c.startswith(f"{UPLOAD_COOKIE}=") and "Max-Age=0" in c for c in r.headers.get_list("set-cookie"))


OPENED = "This company copy was already restored. Celerp opened the existing company; no duplicate was created."
DISCONNECTED = "Connections to other services, such as stores and accounting, are not included in a company copy. Connect each one again in Web Access."


async def test_done_page_states_outcome_from_server(ui, real_engine, real_client):
    """The page after a restore says what the API reported: a new company, or the existing
    one opened with no duplicate. The address cannot make it claim either."""
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    preview = _page(await _upload_and_preview(ui, base, data))
    r = await ui.post(f"{base}/restore", data=_hidden(preview))
    restored_tok = _cookie(r, "celerp_token")
    page = _page(await _follow(ui, r))
    assert "Company restored" in page and DISCONNECTED in page and OPENED not in page

    ui.cookies.set("celerp_token", tok)
    preview = _page(await _upload_and_preview(ui, base, data))
    page = _page(await _follow(ui, await ui.post(f"{base}/restore", data=_hidden(preview))))
    assert OPENED in page and DISCONNECTED not in page
    assert len(await _company_ids(real_engine)) == 2

    ui.cookies.set("celerp_token", restored_tok)
    page = _page(await ui.get(f"{base}/done?restored=1&team_members=5&reactivated=1"))
    assert DISCONNECTED not in page and OPENED not in page and "team members" not in page.lower()


async def test_done_page_links_users_and_roles_when_roles_follow_installation(ui, real_engine):
    """When a new company works under this installation's role permissions, the done page links to Users & Roles."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    base = "/setup/new-company/restore-backup"
    ui.cookies.set("celerp_company_backup_outcome", "created:0:0:destination:", path=base)
    page = _page(await ui.get(f"{base}/done"))
    assert _link(page, "/settings/general?tab=users", "Review Users & Roles")
    ui.cookies.set("celerp_company_backup_outcome", "created:0:0:source:", path=base)
    assert "/settings/general?tab=users" not in _page(await ui.get(f"{base}/done"))


# ── Lost responses and unreachable service ───────────────────────────────────

NOT_CONFIRMED = t("company_backup.not_confirmed")


async def test_lost_response_after_commit_resolves_to_same_company(routed_ui, real_engine, real_client):
    """The restore commits but its answer never arrives: the page does not say nothing was
    written, and checking again opens the company that was made, without a duplicate."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    preview = _page(await _upload_and_preview(ui, base, data))

    async def commit_then_drop(request):
        await router._real.handle_async_request(request)
        raise httpx.ReadError("connection dropped")

    router.overrides[("POST", "/company-backups/restore")] = commit_then_drop
    r = await ui.post(f"{base}/restore", data=_hidden(preview))
    page = _page(r)
    assert NOT_CONFIRMED in page and NOTHING_WRITTEN not in page
    assert "http://" not in page and "Check again" in page
    assert len(await _company_ids(real_engine)) == 2

    del router.overrides[("POST", "/company-backups/restore")]
    r = await ui.post(f"{base}/restore", data=_hidden(page))
    assert r.status_code == 303, r.text
    done = _page(await _follow(ui, r))
    assert OPENED in done
    assert len(await _company_ids(real_engine)) == 2


async def test_lost_response_before_commit_retry_restores_once(routed_ui, real_engine, real_client):
    """The restore never reaches the service: checking again performs it, once."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    preview = _page(await _upload_and_preview(ui, base, data))

    def unreachable(request):
        raise httpx.ConnectError("refused")

    router.overrides[("POST", "/company-backups/restore")] = unreachable
    page = _page(await ui.post(f"{base}/restore", data=_hidden(preview)))
    assert t("api.unreachable", "en") in page and "http://" not in page
    assert len(await _company_ids(real_engine)) == 1

    del router.overrides[("POST", "/company-backups/restore")]
    r = await ui.post(f"{base}/restore", data=_hidden(page))
    assert r.status_code == 303, r.text
    assert "Company restored" in _page(await _follow(ui, r))
    assert len(await _company_ids(real_engine)) == 2


async def test_read_refusal_names_the_chosen_file(ui, real_engine):
    """A file that is not a company backup is refused naming the file the owner chose."""
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    r = await ui.post("/settings/restore-backup/read",
                      files={"file": ("ledger-notes.txt", b"plain text, not a backup", "text/plain")})
    assert r.status_code >= 400
    assert "ledger-notes.txt: " in _page(r)


# ── Modules the backup needs ─────────────────────────────────────────────────

def _json(status: int, body: dict):
    return lambda request: httpx.Response(status, json=body)


async def test_prepare_modules_restarts_then_returns_to_backup(routed_ui, real_engine, real_client):
    """Turning on a module the backup needs waits for Celerp to restart and comes back to
    the same staged backup; with no restart needed it goes straight back."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    await _upload_and_preview(ui, base, data)

    router.overrides[("POST", "/company-backups/prepare")] = _json(202, {"restart": True, "restarting": True})
    r = await ui.post(f"{base}/prepare", data={"consent": ["acme-widgets"]})
    assert r.status_code == 303 and r.headers["location"] == f"{base}/restarting"
    sent = json.loads(router.requests[-1].content)
    assert sent["consent"] == ["acme-widgets"] and sent["upload_token"]
    page = (await ui.get(f"{base}/restarting")).text
    assert f"{base}/staged" in page and "X-Celerp-Staged" in page

    r = await ui.get(f"{base}/staged")
    assert r.status_code == 200 and r.headers.get("x-celerp-staged") == "1"
    assert "Alpha Trading" in _page(r)

    router.overrides[("POST", "/company-backups/prepare")] = _json(200, {"restart": False, "restarting": False})
    r = await ui.post(f"{base}/prepare")
    assert r.status_code == 303 and r.headers["location"] == f"{base}/staged"


async def test_preview_offers_prepare_for_disabled_module(routed_ui, real_engine, real_client):
    """A module installed but turned off is named with a step to turn it on, asking
    consent for one from outside Celerp; there is no restore until it is ready."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"

    async def needs_module(request):
        real = await router._real.handle_async_request(request)
        body = json.loads(await real.aread())
        body.update(modules_ready=False, modules=[
            {"name": "acme-widgets", "label": "Acme Widgets", "status": "enable_required", "first_party": False},
            {"name": "celerp-labels", "label": "Labels", "status": "enable_required", "first_party": True}])
        return httpx.Response(200, json=body)

    router.overrides[("POST", "/company-backups/read")] = needs_module
    r = await ui.post(f"{base}/read", files={"file": ("alpha.celerp-company", data, "application/octet-stream")})
    page = _page(r)
    assert "Installed but turned off." in page
    assert f'action="{base}/prepare"' in page and f'action="{base}/restore"' not in page
    assert re.search(r'name="consent"[^>]*value="acme-widgets"', page)
    assert 'value="celerp-labels"' not in page


_NEEDS_TWO = [
    {"name": "acme-widgets", "label": "Acme Widgets", "status": "enable_required", "first_party": True},
    {"name": "celerp-labels", "label": "Labels", "status": "enable_required", "first_party": True}]


def _needing(router, modules=_NEEDS_TWO, **extra):
    """The staged copy as the real API previews it, needing ``modules`` (and ``extra``)."""
    async def hook(request):
        real = await router._real.handle_async_request(request)
        body = json.loads(await real.aread())
        if real.status_code == 200:
            body.update(modules_ready=not modules, modules=modules, **extra)
        return httpx.Response(real.status_code, json=body)
    return hook


async def test_module_step_offers_enable_and_open_with_the_name(routed_ui, real_engine, real_client):
    """The step that turns modules on also opens the copy: it carries the name to open it
    under and the preview it confirms, and the restart page says the company opens next."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    router.overrides[("POST", "/company-backups/read")] = _needing(router)
    page = _page(await _upload_and_preview(ui, base, data))
    assert "Enable and open" in page
    assert "Acme Widgets" in page and "Labels" in page
    assert re.search(r'name="company_name"[^>]*value="Alpha Trading \(Restored\)"', page)
    fingerprint = _hidden(page)["plan_fingerprint"]
    assert fingerprint

    router.overrides[("POST", "/company-backups/prepare")] = _json(202, {"restart": True, "restarting": True})
    r = await ui.post(f"{base}/prepare", data={"plan_fingerprint": fingerprint, "company_name": "Alpha Copy",
                                               "open": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"{base}/restarting?open=1"
    sent = json.loads(router.requests[-1].content)
    assert sent["open"] == {"plan_fingerprint": fingerprint, "company_name": "Alpha Copy"}
    page = _page(await ui.get(f"{base}/restarting?open=1"))
    assert "The company opens when it is ready." in page


async def test_declining_the_modules_writes_nothing_and_names_them(routed_ui, real_engine, real_client, tmp_path):
    """Declining deletes the upload, opens nothing, and says which modules the copy needs."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    before = await _company_ids(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    router.overrides[("POST", "/company-backups/read")] = _needing(router)
    page = _page(await _upload_and_preview(ui, base, data))
    assert f'action="{base}/decline"' in page and "Don't enable them" in page
    router.overrides[("GET", "/company-backups/staged")] = _needing(router)
    r = await ui.post(f"{base}/decline")
    assert r.status_code == 200, r.text
    assert ("Nothing was opened. This company copy needs these modules: Acme Widgets, Labels.") in _page(r)
    assert _stages(tmp_path) == []
    assert any(c.startswith(f"{UPLOAD_COOKIE}=") and "Max-Age=0" in c for c in r.headers.get_list("set-cookie"))
    assert await _company_ids(real_engine) == before


async def test_staged_copy_marked_open_now_opens_it(routed_ui, real_engine, real_client):
    """Back from the restart, a copy the owner chose Enable and open for shows a page that
    says it is opening and posts the opening; the page load itself opens nothing."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    await _upload_and_preview(ui, base, data)
    router.overrides[("GET", "/company-backups/staged")] = _needing(
        router, modules=[], open_now={"company_name": "Alpha Copy"})
    before = await _company_ids(real_engine)
    r = await ui.get(f"{base}/staged")
    assert r.status_code == 200 and r.headers["X-Celerp-Staged"] == "1"
    page = _page(r)
    assert t("company_backup.opening_chosen", name="Alpha Copy") in page
    assert f'action="{base}/staged"' in page and 'method="post"' in page
    assert await _company_ids(real_engine) == before, "loading the page opens nothing"
    r = await ui.post(f"{base}/staged")
    assert r.status_code == 303 and r.headers["location"] == f"{base}/done", r.text
    async with real_engine.connect() as conn:
        names = (await conn.execute(text("SELECT name FROM companies"))).scalars().all()
    assert "Alpha Copy" in names


async def test_staged_copy_held_after_restart_says_why(routed_ui, real_engine, real_client):
    """A copy whose preview changed while Celerp restarted is shown again with the reason,
    never opened on the owner's behalf."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    before = await _company_ids(real_engine)
    data = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    base = "/settings/restore-backup"
    await _upload_and_preview(ui, base, data)
    router.overrides[("GET", "/company-backups/staged")] = _needing(router, modules=[], open_held=True)
    r = await ui.get(f"{base}/staged")
    assert r.status_code == 200
    assert t("company_backup.open_held") in _page(r)
    assert f'action="{base}/restore"' in _page(r)
    assert await _company_ids(real_engine) == before


async def test_first_run_module_step_keeps_turn_on_modules(routed_ui, real_engine, real_client, code_config):
    """On a fresh installation the account is created with the opening, and a password is
    never kept across a restart, so the step turns modules on and shows the copy again."""
    ui, router = routed_ui
    _, _, tok = await _setup(real_engine)
    data = await download(real_client, tok)
    async with real_engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))
    base = "/setup/restore-backup"
    router.overrides[("POST", "/company-backups/bootstrap/read")] = _needing(router)
    page = _page(await _upload_and_preview(ui, base, data, setup_code=code_config))
    assert "Turn on modules" in page and "Enable and open" not in page
    assert f'action="{base}/decline"' in page

OPENING = "celerp_handoff_opening"


async def _status_of(ui, base: str = "/settings/restore-backup") -> dict:
    r = await ui.get(f"{base}/opening/status")
    assert r.status_code == 200, r.text
    return r.json()


async def _until(ui, state: str, base: str = "/settings/restore-backup") -> dict:
    import asyncio
    for _ in range(200):
        status = await _status_of(ui, base)
        if status["state"] == state:
            return status
        await asyncio.sleep(0.02)
    raise AssertionError(f"opening never reached {state}: {status}")


def _verified(monkeypatch, backup: bytes, link: str):
    from celerp.services import company_handoff_transfer
    from ui.routes import company_backup as page

    async def verify(copy_id, proof, digest):
        assert copy_id == str(page.parse_handoff_link(link).copy_id)
        assert proof == page.parse_handoff_link(link).proof_hash
        assert digest == __import__("hashlib").sha256(backup).hexdigest()
    monkeypatch.setattr(company_handoff_transfer, "verify_company_handoff", verify)


async def test_open_link_runs_as_background_job(ui, real_engine, real_client, monkeypatch, tmp_path):
    """Opening a company copy link answers at once and downloads in the background; the
    finished copy is staged for the usual preview and nothing is imported."""
    import asyncio
    from celerp.services.company_handoff_links import handoff_page_link
    from ui.routes import company_backup as page

    _, _, tok = await _setup(real_engine)
    backup = await download(real_client, tok)
    ui.cookies.set("celerp_token", tok)
    link = handoff_page_link(uuid.uuid4(), bytes(range(32)))
    release = asyncio.Event()
    called = []

    async def retrieve(value, destination, progress=None):
        called.append(value)
        progress("requesting")
        progress("downloading", 400, 1000)
        await release.wait()
        progress("downloading", 1000, 1000)
        progress("decrypting")
        destination.write_bytes(backup)

    monkeypatch.setattr(page, "download_company_handoff", retrieve)
    _verified(monkeypatch, backup, link)
    before = await _company_ids(real_engine)
    r = await ui.post("/settings/restore-backup/read", data={"handoff_link": link})
    assert r.status_code == 303 and r.headers["location"] == "/settings/restore-backup/opening"
    assert OPENING in r.headers.get("set-cookie", "")
    assert link not in r.text and link not in r.headers.get("set-cookie", "")
    running = await _until(ui, "running")
    assert running["percent"] == 40
    release.set()
    await _until(ui, "ready")
    r = await ui.get("/settings/restore-backup/opened")
    assert r.status_code == 200, r.text
    assert "Alpha Trading" in _page(r)
    assert NOTHING_WRITTEN in _page(r)
    assert UPLOAD_COOKIE in r.headers.get("set-cookie", "")
    assert link not in r.text
    assert called == [link]
    assert await _company_ids(real_engine) == before
    assert not list(tmp_path.glob("celerp-handoff-*"))


async def test_open_link_progress_page(ui, real_engine, real_client, monkeypatch):
    """The opening page shows what is happening and how far it has got, can be cancelled,
    and a copy that cannot be opened says why and how to try again."""
    import asyncio
    from celerp.services.company_handoff_links import handoff_page_link
    from ui.routes import company_backup as page

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    link = handoff_page_link(uuid.uuid4(), bytes(range(32)))
    hold = asyncio.Event()

    async def slow(value, destination, progress=None):
        progress("downloading", 250, 1000)
        await hold.wait()

    monkeypatch.setattr(page, "download_company_handoff", slow)
    await ui.post("/settings/restore-backup/read", data={"handoff_link": link})
    await _until(ui, "running")
    r = await ui.get("/settings/restore-backup/opening")
    text_ = _page(r)
    assert r.status_code == 200
    assert t("company_backup.opening_downloading", percent=25) in text_
    assert t("company_backup.opening_nothing_imported") in text_
    assert 'class="import-progress"' in r.text
    assert re.search(r'<form[^>]*action="/settings/restore-backup/opening/cancel"', r.text)

    # Another browser has no opening to look at.
    import httpx as _httpx
    async with _httpx.AsyncClient(transport=ui._transport, base_url="http://localhost") as other:
        other.cookies.set("celerp_token", tok)
        r = await other.get("/settings/restore-backup/opening/status")
        assert r.status_code == 404

    r = await ui.post("/settings/restore-backup/opening/cancel")
    assert r.status_code == 303 and r.headers["location"] == "/settings/restore-backup"
    r = await ui.get("/settings/restore-backup/opening/status")
    assert r.status_code == 404

    async def unavailable(value, destination, progress=None):
        from celerp.services.company_handoff_transfer import HandoffDownloadError
        raise HandoffDownloadError("expired")

    monkeypatch.setattr(page, "download_company_handoff", unavailable)
    await ui.post("/settings/restore-backup/read", data={"handoff_link": link})
    failed = await _until(ui, "failed")
    assert failed["message"] == t("company_backup.handoff_unavailable")
    r = await ui.get("/settings/restore-backup/opened")
    assert t("company_backup.handoff_unavailable") in _page(r)
    assert 'name="handoff_link"' in r.text


async def test_invalid_handoff_link_is_not_downloaded(ui, real_engine, monkeypatch):
    from ui.routes import company_backup as page

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    async def unexpected(*args, **kwargs):
        raise AssertionError("Invalid link must not reach the network")
    monkeypatch.setattr(page, "download_company_handoff", unexpected)
    before = await _company_ids(real_engine)
    r = await ui.post("/settings/restore-backup/read", data={
        "handoff_link": "https://evil.example/handoffs/abcd/open#" + "A" * 43,
    })
    assert r.status_code == 200
    assert "Check the link or ask for a new one" in _page(r)
    assert await _company_ids(real_engine) == before


async def test_company_handoff_send_form_retries_original_operation(ui, real_engine, monkeypatch):
    from ui.routes.company_backup import SEND
    from ui import api_client as api
    from celerp.services.company_handoff_links import handoff_page_link

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    r = await ui.get(SEND)
    assert r.status_code == 200, r.text
    initial = _hidden(_page(r))
    assert len(initial["key"]) == 43
    payload = {**initial, "firm_name": "Example Accounting", "recipient": "client@example.com",
               "reply_to": "firm@example.com", "note": "Your books"}
    calls = []

    async def send(token, body):
        calls.append((token, body))
        if len(calls) == 1:
            raise api.APIError(502, "Delivery not confirmed")
        secret = base64.urlsafe_b64decode(body["key"] + "=")
        return {"id": str(uuid.uuid4()), "link": handoff_page_link(uuid.uuid4(), secret)}

    async def no_activity(token):
        raise api.APIError(502, "Cloud unavailable")

    monkeypatch.setattr(api, "company_backup_send_handoff", send)
    monkeypatch.setattr(api, "company_backup_handoffs", no_activity)
    refused = await ui.post(SEND, data=payload)
    assert refused.status_code == 502
    assert _hidden(_page(refused))["key"] == initial["key"]
    assert _hidden(_page(refused))["operation_id"] == initial["operation_id"]
    sent = await ui.post(SEND, data=payload)
    assert sent.status_code == 200 and SENT_TO_CLIENT in _page(sent)
    assert [call[1]["operation_id"] for call in calls] == [initial["operation_id"]] * 2
    assert all(call[1]["key"] == initial["key"] for call in calls)


async def test_send_link_without_connect(ui, real_engine, monkeypatch):
    """No Celerp account: the send page keeps every typed value, offers a free account in
    place and resumes the same send once it is verified; Download copy is the other way
    out. Connect is never mentioned."""
    from ui.routes.company_backup import DOWNLOAD, SEND
    from ui import api_client as api

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    initial = _hidden(_page(await ui.get(SEND)))
    payload = {**initial, "firm_name": "Example Accounting", "recipient": "client@example.com",
               "reply_to": "firm@example.com", "note": "Your books"}

    async def no_account(token, body):
        raise api.APIError(428, "Sign in to a free Celerp account to send a company copy link.")

    async def methods(token):
        return {"google": False}
    monkeypatch.setattr(api, "company_backup_send_handoff", no_account)
    monkeypatch.setattr(api, "account_methods", methods)
    r = await ui.post(SEND, data=payload)
    page = _page(r)
    assert r.status_code == 428
    assert 'id="celerp-account-panel"' in page and 'value="copy-send"' in page
    assert 'id="copy-send-form"' in page
    assert _hidden(page)["key"] == initial["key"] and "Example Accounting" in page
    assert f'href="{DOWNLOAD}"' in page
    assert "Connect" not in page


async def test_send_form_prefills_the_last_firm(ui, real_engine, monkeypatch):
    """The firm name and reply address start as the last send's, editable; the client's
    email is always typed. Without the last send the fields start empty."""
    from ui.routes.company_backup import SEND
    from ui import api_client as api

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)

    async def last(token):
        return {"firm_name": "North Ledger", "reply_to": "desk@north.example"}
    monkeypatch.setattr(api, "company_backup_sender", last)

    def field(page: str, name: str) -> str:
        tag = re.search(rf'<input[^>]*id="{name}"[^>]*>', page).group(0)
        value = re.search(r'value="([^"]*)"', tag)
        return value.group(1) if value else ""

    page = _page(await ui.get(SEND))
    assert field(page, "firm_name") == "North Ledger"
    assert field(page, "reply_to") == "desk@north.example"
    assert field(page, "recipient") == ""

    async def unavailable(token):
        raise api.APIError(502, "Unavailable")
    monkeypatch.setattr(api, "company_backup_sender", unavailable)
    page = _page(await ui.get(SEND))
    assert 'id="copy-send-form"' in page and field(page, "firm_name") == ""


async def test_send_form_says_what_celerp_keeps(ui, real_engine, monkeypatch):
    """Before sending, the form states that Celerp holds only the encrypted file and
    passes the link, which holds the key, by email without keeping it."""
    from ui.routes.company_backup import SEND
    from ui import api_client as api

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)

    async def last(token):
        return {}
    monkeypatch.setattr(api, "company_backup_sender", last)
    page = _page(await ui.get(SEND))
    form = page.split('id="copy-send-form"', 1)[1].split("</form>", 1)[0]
    assert ("Celerp encrypts this copy on your computer and keeps only the encrypted file, until the "
            "link expires or you revoke it. The key that opens it is in the link, which Celerp passes "
            "to the recipient by email in transit and does not keep.") in form


async def test_copy_send_resumes_the_same_form_after_sign_in():
    from fasthtml.common import to_xml
    from ui.routes import account
    assert account._next_from("copy-send") == "copy-send"
    script = to_xml(account._resume_parts("copy-send")[0])
    assert "copy-send-form" in script and "requestSubmit" in script


async def test_company_handoff_activity_revoke_and_navigation(ui, real_engine, monkeypatch):
    from ui.routes import company_backup as route
    from ui import api_client as api

    _, _, tok = await _setup(real_engine, name="Company handoff list")
    ui.cookies.set("celerp_token", tok)
    copy_id = str(uuid.uuid4())
    calls = []

    async def list_activity(token):
        assert token == tok
        return {"items": [{"id": copy_id, "status": "sent", "opened": True,
                           "downloaded": False, "company_opened": False,
                           "expires_at": "2026-10-25T00:00:00+00:00"}]}

    async def revoke(token, which):
        calls.append((token, which))
        return {"revoked": True}

    monkeypatch.setattr(api, "company_backup_handoffs", list_activity)
    monkeypatch.setattr(api, "company_backup_revoke_handoff", revoke)
    sent = await ui.get(route.SEND)
    assert sent.status_code == 200 and route.HANDOFFS in _page(sent)
    listed = await ui.get(route.HANDOFFS)
    assert listed.status_code == 200 and "Opened" in _page(listed)
    assert f"{route.HANDOFFS}/{copy_id}/revoke" in _page(listed)
    revoked = await ui.post(f"{route.HANDOFFS}/{copy_id}/revoke")
    assert revoked.status_code == 303 and revoked.headers["location"] == route.HANDOFFS
    assert calls == [(tok, copy_id)]


def _copy_row(**over) -> dict:
    row = {"id": str(uuid.uuid4()), "status": "sent", "sent_at": "2026-10-10T08:00:00+00:00",
           "expires_at": "2026-10-24T08:00:00+00:00", "opened": False, "downloaded": False,
           "company_opened": False, "stored": True, "can_restore": False, "renewable": True, "key_missing": False,
           "firm_name": "Example Accounting", "recipient": "client@example.com", "link": None}
    return {**row, **over}


async def test_new_link_is_offered_only_for_a_copy_whose_key_is_kept(ui, real_engine, monkeypatch):
    """A stored copy whose key this computer no longer holds has no New link; the row
    says why and what to do, and Resend is still there."""
    from ui.routes import company_backup as route
    from ui import api_client as api

    _, _, tok = await _setup(real_engine, name="Company copies keys")
    ui.cookies.set("celerp_token", tok)
    kept = _copy_row(link="https://relay.celerp.com/handoffs/a/open#" + "A" * 43)
    lost = _copy_row(sent_at="2026-10-12T08:00:00+00:00", renewable=False, key_missing=True,
                     recipient="other@example.com")

    async def listed(token):
        return {"items": [kept, lost]}
    monkeypatch.setattr(api, "company_backup_handoffs", listed)
    page = _page(await ui.get(route.HANDOFFS))
    assert f'{route.HANDOFFS}/{kept["id"]}/new-link' in page
    assert f'{route.HANDOFFS}/{lost["id"]}/new-link' not in page
    assert t("company_backup.copies_key_missing") in page
    assert "recipient=other%40example.com" in page


async def test_handoffs_table_header_dates_order_revoke_undo(ui, real_engine, monkeypatch):
    from ui.routes import company_backup as route
    from ui import api_client as api

    _, _, tok = await _setup(real_engine, name="Company copies table")
    ui.cookies.set("celerp_token", tok)
    older = _copy_row(link="https://relay.celerp.com/handoffs/a/open#" + "A" * 43)
    newer = _copy_row(sent_at="2026-10-15T08:00:00+00:00", expires_at="2026-10-29T08:00:00+00:00",
                      status="revoked", can_restore=True, recipient=None, firm_name="Second Firm")
    rows = {"items": [older, newer]}
    calls = []

    async def listed(token):
        return rows

    async def revoke(token, which):
        calls.append(("revoke", which))
        return {**older, "status": "revoked", "can_restore": True, "link": None}

    async def restore(token, which):
        calls.append(("restore", which))
        return {**newer, "status": "sent", "can_restore": False}

    async def new_link(token, which, operation):
        calls.append(("new-link", which, operation))
        if which == newer["id"]:
            raise api.APIError(410, "This copy is no longer stored. Use Resend to send a new copy")
        older["link"] = "https://relay.celerp.com/handoffs/b/open#" + "A" * 43
        return dict(older)

    monkeypatch.setattr(api, "company_backup_handoffs", listed)
    monkeypatch.setattr(api, "company_backup_revoke_handoff", revoke)
    monkeypatch.setattr(api, "company_backup_restore_handoff", restore)
    monkeypatch.setattr(api, "company_backup_new_handoff_link", new_link)

    page = _page(await ui.get(route.HANDOFFS))
    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    for key in ("company_backup.copies_sent_to", "th.status", "company_backup.copies_sent",
                "company_backup.copies_expires", "th.actions"):
        assert t(key) in head
    # Localized dates, never the raw timestamp; newest copy first; "--" for a missing value.
    assert "2026-10-24T08" not in page and str(format_value(older["expires_at"], "date")) in page
    body = page[page.index("<tbody>"):]
    assert body.index("Second Firm") < body.index("client@example.com")
    assert "<td>--</td>" in body
    # Copy link, New link, Resend and Revoke on the live copy; Undo on the one just revoked.
    assert f'data-copy-text="{older["link"]}"' in page
    assert f'hx-post="{route.HANDOFFS}/{older["id"]}/new-link"' in page
    assert f'hx-post="{route.HANDOFFS}/{older["id"]}/revoke"' in page and "hx-confirm" in page
    assert f'hx-post="{route.HANDOFFS}/{newer["id"]}/restore"' in page
    assert f"{route.SEND}?firm_name=Example+Accounting&amp;recipient=client%40example.com" in \
        (await ui.get(route.HANDOFFS)).text
    assert t("company_backup.copies_revoke_reason") in page

    swap = {"HX-Request": "true"}
    revoked = await ui.post(f"{route.HANDOFFS}/{older['id']}/revoke", headers=swap)
    assert revoked.status_code == 200 and 'id="copies-list"' in revoked.text
    assert "<html" not in revoked.text.lower()  # swapped in place, no reload
    assert t("company_backup.copies_revoked") in _page(revoked)
    undone = await ui.post(f"{route.HANDOFFS}/{newer['id']}/restore", headers=swap)
    assert undone.status_code == 200 and t("company_backup.copies_restored") in _page(undone)
    operation = str(uuid.uuid4())
    renewed = await ui.post(f"{route.HANDOFFS}/{older['id']}/new-link", headers=swap,
                            data={"operation_id": operation})
    assert t("company_backup.copies_new_link_done") in _page(renewed)
    assert "Celerp has not emailed it. Use Copy link to send it to them." in _page(renewed)
    assert "handoffs/b/open" in renewed.text
    refused = await ui.post(f"{route.HANDOFFS}/{newer['id']}/new-link", headers=swap,
                            data={"operation_id": str(uuid.uuid4())})
    assert refused.status_code == 200 and "Use Resend to send a new copy" in _page(refused)
    assert calls[:3] == [("revoke", older["id"]), ("restore", newer["id"]),
                         ("new-link", older["id"], operation)]

    # Resend opens the send form filled in for the same firm and recipient.
    resend = _page(await ui.get(route.SEND, params={"firm_name": "Example Accounting",
                                                   "recipient": "client@example.com"}))
    assert 'value="Example Accounting"' in resend and 'value="client@example.com"' in resend


async def test_switch_to_migrated_company_before_send(ui, real_engine):
    user, first, access = await _setup(real_engine, name="First books")
    second = await company(real_engine, user, "Migrated books", "migration-switch-target")
    ui.cookies.set("celerp_token", access)
    action = await ui.get(f"/switch-company/{second}?next=/settings/company-handoff")
    assert action.status_code == 302
    assert action.headers["location"] == "/settings/company-handoff"
    assert _claims(_cookie(action, "celerp_token"))["company_id"] == str(second)
    # Never make company switching a general-purpose redirect endpoint.
    for target in ("/accounting/journal/new", "/settings/users/new"):
        landed = await ui.get(f"/switch-company/{second}?next={target}")
        assert landed.status_code == 302 and landed.headers["location"] == target
    other = await ui.get(f"/switch-company/{first}?next=https://bad.example")
    assert other.status_code == 302 and other.headers["location"] == "/"
    encoded = await ui.get(f"/switch-company/{first}?next=%2F%2Fevil.example")
    assert encoded.status_code == 302 and encoded.headers["location"] == "/"


async def _sent_page(ui, real_engine, monkeypatch, *, activity):
    """Send a company copy through the form; *activity* answers the copies-sent lookup."""
    from ui.routes.company_backup import SEND
    from ui import api_client as api
    from celerp.services.company_handoff_links import handoff_page_link

    _, _, tok = await _setup(real_engine, name="Ready page books")
    ui.cookies.set("celerp_token", tok)
    form = _hidden(_page(await ui.get(SEND)))
    copy_id = str(uuid.uuid4())
    link = handoff_page_link(uuid.UUID(copy_id), base64.urlsafe_b64decode(form["key"] + "="))

    async def send(token, body):
        return {"id": copy_id, "link": link}

    async def handoffs(token):
        return activity(copy_id)

    monkeypatch.setattr(api, "company_backup_send_handoff", send)
    monkeypatch.setattr(api, "company_backup_handoffs", handoffs)
    sent = await ui.post(SEND, data={**form, "firm_name": "Example Accounting",
                                     "recipient": "client@example.com",
                                     "reply_to": "firm@example.com", "note": ""})
    assert sent.status_code == 200, sent.text
    return sent, link


async def test_sent_page_has_copy_and_next_steps(ui, real_engine, monkeypatch):
    from ui.routes.company_backup import DOWNLOAD, HANDOFFS
    from ui.routes.migrations import COMPANY

    raw, link = await _sent_page(ui, real_engine, monkeypatch, activity=lambda copy_id: {"items": [
        {"id": copy_id, "status": "sent", "opened": False, "downloaded": False,
         "company_opened": False, "expires_at": "2026-10-30T09:00:00+00:00"}]})
    page, raw = _page(raw), raw.text
    assert "<h1" in page and SENT_TO_CLIENT in page
    # The three actions, in the journey's order.
    copy_at = raw.index(f'data-copy-text="{link}"')
    download_at = page.index(f'href="{DOWNLOAD}"')
    another_at = page.index(f'href="{COMPANY.base}"')
    assert "Copy link" in raw[copy_at:copy_at + 300]
    assert _link(page, DOWNLOAD, "Download copy") and _link(page, COMPANY.base, "Move another company")
    assert raw.index("Copy link") < raw.index("Download copy") < raw.index("Move another company")
    assert download_at < another_at
    # Next steps: what the client gets, when the link expires, where to watch, how to resend.
    assert ("client@example.com receives an email from Example Accounting with the link. "
            "Opening it gives them their own Celerp with this company in it.") in page
    assert any(f"The link expires on {d}." in page for d in _dates("2026-10-30T09:00:00+00:00"))
    assert _link(page, HANDOFFS, "Company copies")
    assert "See when they open it in Company copies." in re.sub(r"<[^>]+>", "", page)
    assert f'<a href="{HANDOFFS}">To send a new link, open Company copies and use New link.</a>' in page


async def test_company_copy_ready_state(ui, real_engine, monkeypatch):
    """Without the copies-sent lookup the page still states the 14-day rule, never a guessed date."""
    from ui import api_client as api

    def unavailable(copy_id):
        raise api.APIError(502, "Cloud unavailable")

    raw, link = await _sent_page(ui, real_engine, monkeypatch, activity=unavailable)
    page, raw = _page(raw), raw.text
    assert SENT_TO_CLIENT in page
    assert f'data-copy-text="{link}"' in raw
    assert "The link expires 14 days after it was sent." in page
    assert "The link expires on" not in page
    assert "Company copy sent" not in page


async def _demo_company(real_client, real_engine):
    """The demo company as seed_demo builds it: postings, money owed both ways."""
    from test_company_copy_round_trip import seed_demo
    registered = await real_client.post("/auth/register", json={
        "company_name": "Demo Trading", "email": "demo-owner@example.test",
        "name": "Demo Owner", "password": "demopw1234"})
    assert registered.status_code == 200, registered.text
    tok = registered.json()["access_token"]
    await seed_demo.seed_items(real_client, tok)
    contacts = await seed_demo.seed_contacts(real_client, tok)
    await seed_demo.seed_docs(real_client, tok, contacts, "invoice", 8)
    await seed_demo.seed_docs(real_client, tok, contacts, "purchase_order", 5)
    return tok


async def _key_totals(real_client, tok: str) -> tuple[str, str, str]:
    from ui.components.table import fmt_money
    headers = {"Authorization": f"Bearer {tok}"}
    currency = (await real_client.get("/companies/me", headers=headers)).json().get("currency")
    tb = (await real_client.get("/accounting/trial-balance", headers=headers)).json()
    ar = (await real_client.get("/reports/ar-aging", headers=headers)).json()
    ap = (await real_client.get("/reports/ap-aging", headers=headers)).json()
    return (fmt_money(tb["total_debit"], currency), fmt_money(sum(ar["buckets"].values()), currency),
            fmt_money(sum(ap["buckets"].values()), currency))


async def test_restore_success_shows_reconciled_totals(ui, real_engine, real_client, monkeypatch, tmp_path):
    """Opening a company copy a firm prepared ends on the company's own key balances
    (trial balance, owed to you, you owe), names the firm, and says the company is free to
    use locally with no time limit. A plain copy shows the same balances without a firm."""
    from celerp.services import company_handoff_sender
    tok = await _demo_company(real_client, real_engine)
    expected = await _key_totals(real_client, tok)
    assert all(figure != "--" for figure in expected)
    sent = tmp_path / "sent.celerp-company"

    async def send(**kwargs):
        await kwargs["export"](sent)
        return {"id": str(uuid.uuid4()), "link": "synthetic-link"}

    monkeypatch.setattr(company_handoff_sender, "send_company_backup", send)
    r = await real_client.post("/company-backups/send-handoff", headers={"Authorization": f"Bearer {tok}"}, json={
        "operation_id": str(uuid.uuid4()), "key": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "firm_name": "North Ledger", "recipient": "client@example.com", "reply_to": "desk@north.example",
        "note": ""})
    assert r.status_code == 200, r.text

    base = "/settings/restore-backup"
    for data, firm in ((sent.read_bytes(), True), (await download(real_client, tok), False)):
        ui.cookies.set("celerp_token", tok)
        preview = _page(await _upload_and_preview(ui, base, data))
        r = await ui.post(f"{base}/restore", data=_hidden(preview))
        restored_tok = _cookie(r, "celerp_token")
        page = _page(await _follow(ui, r))
        assert "Free to use locally" not in page  # a work screen, not sales copy
        # A trial balance is not one amount: the row names the figure it shows.
        for label, figure in zip(("Total debits on the trial balance", "Owed to you", "You owe"),
                                 await _key_totals(real_client, restored_tok)):
            assert re.search(rf"<td>{label}</td>\s*<td[^>]*>{re.escape(figure)}</td>", page), (label, figure)
        assert await _key_totals(real_client, restored_tok) == expected
        assert ("Your company is ready." in page and "Prepared for you by North Ledger." in page) is firm
        assert ("Company restored" in page) is not firm


async def test_copies_page_shows_the_cloud_refusal(ui, real_engine, monkeypatch):
    """A refusal the owner can act on is shown in its own words; an unconfirmed result
    keeps the general message. RED before the change: every refusal read as unavailable."""
    from ui import api_client as api
    from ui.routes import company_backup as route
    _, _, tok = await _setup(real_engine, name="Company handoff refusal")
    ui.cookies.set("celerp_token", tok)
    refusal = {"error": api.APIError(428, "Sign in to a free Celerp account to send a company copy link.")}

    async def refused(token):
        raise refusal["error"]

    monkeypatch.setattr(api, "company_backup_handoffs", refused)
    page = _page(await ui.get(route.HANDOFFS))
    assert "Sign in to a free Celerp account" in page
    refusal["error"] = api.APIError(502, "Could not confirm the company copy. Retry the same operation")
    page = _page(await ui.get(route.HANDOFFS))
    assert t("company_backup.handoffs_unavailable") in page and "Could not confirm" not in page


@pytest.mark.parametrize("path, heading", [("/settings/company-handoff", "Company copies"),
                                           ("/settings/company-handoffs", "Company copies"),
                                           ("/settings/saved-copies", "Saved copies")])
async def test_a_non_owner_is_refused_under_the_page_heading_and_told_whom_to_ask(
        ui, real_engine, monkeypatch, path, heading):
    """Send, the copies list and saved copies refuse a non-owner under their own heading, not the
    open-a-copy one, and name the owner to ask."""
    import ui.routes.migrations as migrations
    import ui.api_client as api
    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    monkeypatch.setattr(migrations, "get_role", lambda request: "admin")

    async def users(token):
        return {"items": [{"name": "Pat Owner", "email": "pat@example.com", "role": "owner", "is_active": True}]}
    monkeypatch.setattr(api, "get_users", users)
    r = await ui.get(path)
    assert r.status_code == 403, r.text
    page = _page(r)
    assert "Only the company owner can make, send or open a company copy." in page
    assert "Ask Pat Owner to do it, or to make you the owner." in page
    assert f'<h1 class="auth-title">{heading}</h1>' in page
    assert "Open a company copy" not in page


async def test_replacing_names_one_changed_record_and_times_a_same_day_copy():
    """One record changed reads as one record, and when the copy and the company here were
    last changed on the same day, the times show which is older."""
    from fasthtml.common import Div, to_xml

    from ui.routes.company_backup import SETTINGS, _replace_section

    def facts(when: str) -> dict:
        return {"documents": 1, "journals": 1, "items": 1, "last_changed": when}

    replace = {"company_name": "Demo Trading Co", "can_replace": True, "changed_after_copy": 1,
               "current": facts("2026-10-17T15:40:00+00:00"), "incoming": facts("2026-10-17T09:05:00+00:00"),
               "older": {"since": "2026-10-17T09:05:00+00:00", "documents": 1}}
    page = _html.unescape(to_xml(Div(*_replace_section(SETTINGS, {}, replace))))
    assert "1 record in Demo Trading Co changed after this copy was made." in page
    assert "1 records" not in page
    assert "2026-10-17 15:40 UTC" in page and "2026-10-17 09:05 UTC" in page


def _kept_link(html: str) -> str:
    """The copy link field's value, which must stay masked."""
    field = re.search(r'<input\b[^>]*name="handoff_link"[^>]*>', html).group(0)
    assert 'type="password"' in field
    assert 'data-reveal="#handoff_link"' in html
    return re.search(r'value="([^"]*)"', field).group(1) if 'value="' in field else ""


async def test_a_refused_link_stays_in_the_field(ui, real_engine, monkeypatch):
    """A link that is refused, at once or after its download fails, comes back in its
    field, masked, with a Show control, so the owner can check what they pasted."""
    from celerp.services.company_handoff_links import handoff_page_link
    from celerp.services.company_handoff_transfer import HandoffDownloadError
    from ui.routes import company_backup as page

    _, _, tok = await _setup(real_engine)
    ui.cookies.set("celerp_token", tok)
    assert _kept_link((await ui.get("/settings/restore-backup")).text) == ""

    pasted = "https://evil.example/handoffs/abcd/open#" + "A" * 43
    r = await ui.post("/settings/restore-backup/read", data={"handoff_link": pasted})
    assert _kept_link(r.text) == pasted

    async def unavailable(value, destination, progress=None):
        raise HandoffDownloadError("expired")

    monkeypatch.setattr(page, "download_company_handoff", unavailable)
    link = handoff_page_link(uuid.uuid4(), secrets.token_bytes(32))
    await ui.post("/settings/restore-backup/read", data={"handoff_link": link})
    await _until(ui, "failed")
    r = await ui.get("/settings/restore-backup/opened")
    assert t("company_backup.handoff_unavailable") in _page(r)
    assert _kept_link(r.text) == link


def test_copies_row_actions_sit_in_one_menu():
    """Each copy's actions open from one menu on its row, so the cell never wraps buttons
    onto two uneven lines; a missing key reads under the status, not as a menu item."""
    from fasthtml.common import to_xml

    from ui.routes import company_backup as route

    live = {"id": "c1", "recipient": "client@example.test", "firm_name": "Harbor Bookkeeping",
            "status": "sent", "opened": True, "downloaded": False, "company_opened": False,
            "sent_at": "2026-10-17", "expires_at": "2026-10-31T00:00:00+00:00",
            "link": "https://relay.example/h/x", "renewable": True}
    lost = {**live, "id": "c2", "link": None, "renewable": False, "key_missing": True}
    page = to_xml(route._copies_table([live, lost]))
    assert page.count('class="row-menu"') == 2
    menu = page[page.index('id="menu-copy-c1"'):]
    menu = menu[:menu.index("</div>")]
    for label in ("company_backup.ready_copy_link", "company_backup.copies_new_link",
                  "company_backup.copies_resend", "btn.revoke"):
        assert t(label) in menu
    assert menu.count('class="row-menu-item') == 4 and "btn--sm" not in menu
    assert t("company_backup.copies_key_missing") in page
    assert t("company_backup.copies_key_missing") not in page[page.index('id="menu-copy-c2"'):].split("</div>")[0]
