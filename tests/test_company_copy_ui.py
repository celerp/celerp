# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Company copy pages (UI side), driven against the real API on committed test data:
make a copy, download it, open it as a new company or as a fresh installation."""

from __future__ import annotations

import html as _html
import re
import zipfile
import io

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from migration_support import code_config, count, maker, real_client, real_engine  # noqa: F401
from test_company_copy import _company, _owner, _token

pytestmark = pytest.mark.asyncio

UPLOAD_COOKIE = "celerp_company_copy_upload"


@pytest_asyncio.fixture
async def ui(real_client, tmp_path, monkeypatch):
    """The UI app, its API calls routed to the real API app on `real_engine`."""
    import celerp.main
    import ui.api_client as api
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    transport = httpx.ASGITransport(app=celerp.main.app)

    def _factory(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url="http://api", headers=merged, transport=transport,
                                 follow_redirects=follow_redirects, timeout=timeout)

    monkeypatch.setattr(api, "_local_client", _factory)
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://localhost", follow_redirects=False) as c:
        yield c


def _page(r: httpx.Response) -> str:
    return re.sub(r"<script\b.*?</script>", "", _html.unescape(r.text), flags=re.S | re.I)


def _link(page: str, href: str, text: str) -> bool:
    return re.search(rf'<a\b[^>]*href="{re.escape(href)}"[^>]*>[^<]*{re.escape(text)}[^<]*</a>', page) is not None


def _cookie(r: httpx.Response, name: str) -> str:
    header = next(c for c in r.headers.get_list("set-cookie") if c.startswith(f"{name}="))
    return header.split(";", 1)[0].split("=", 1)[1].strip('"')


def _hidden(page: str) -> dict:
    return dict(re.findall(r'<input\b[^>]*type="hidden"[^>]*name="(preview_[a-z_]+)"[^>]*value="([^"]*)"', page))


async def _companies(engine) -> int:
    async with engine.connect() as conn:
        return await conn.scalar(text("SELECT count(*) FROM companies"))


async def _make_file(ui, engine, user, company) -> bytes:
    """Make a copy through the pages and download it."""
    ui.cookies.set("celerp_token", await _token(engine, user, company))
    r = await ui.get("/company-copy")
    assert r.status_code == 200, r.text
    page = _page(r)
    for s in ("Create independent company copy", "Only this company is included.", "Alpha Trading",
              "Not included", "snapshot", "Prepared by", "Create company copy"):
        assert s in page, s
    assert _link(page, "/", "Cancel")

    r = await ui.post("/company-copy", data={"prepared_by": "Example Accounting", "run_id": ""})
    assert r.status_code == 200, r.text
    ready = _page(r)
    assert "Company copy ready." in ready
    copy_id = re.search(r'href="/company-copy/([0-9a-f]+)/download"', ready).group(1)
    assert _link(ready, f"/company-copy/{copy_id}/download", "Download copy")
    assert _link(ready, "/setup/new-company/migrate", "Move another company")
    assert "Copy link" not in ready and "Open a company copy" in ready
    assert re.search(r"\bclient", ready, re.I) is None

    r = await ui.get(f"/company-copy/{copy_id}/download")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert ".celerp-company" in r.headers["content-disposition"]
    assert "manifest.json" in zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    return r.content


async def test_make_and_open_copy_pages(ui, real_engine):
    user = await _owner(real_engine)
    alpha = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    data = await _make_file(ui, real_engine, user, alpha)

    r = await ui.get("/setup/new-company")
    assert _link(_page(r), "/setup/new-company/open-copy", "Open a company copy")
    base = "/setup/new-company/open-copy"
    r = await ui.get(base)
    assert r.status_code == 200
    assert 'type="file"' in r.text and f'action="{base}/read"' in r.text

    before = await _companies(real_engine)
    r = await ui.post(f"{base}/read", files={"file": ("alpha.celerp-company", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    preview = _page(r)
    assert "Alpha Trading" in preview and "Prepared for you by Example Accounting" in preview
    assert "Nothing has been written yet." in preview and "Open company copy" in preview
    assert await _companies(real_engine) == before
    upload = _cookie(r, UPLOAD_COOKIE)
    ui.cookies.set(UPLOAD_COOKIE, upload, path=base)
    fields = _hidden(preview)
    assert fields["preview_company_name"] == "Alpha Trading"

    r = await ui.post(f"{base}/open", data=fields)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"{base}/done"
    assert await _companies(real_engine) == before + 1
    ui.cookies.set("celerp_token", _cookie(r, "celerp_token"))

    r = await ui.get(f"{base}/done")
    done = _page(r)
    assert "Your company is ready." in done and "Alpha Trading" in done
    assert "Free to use locally with no time limit." in done
    for label in ("Trial balance debits", "Trial balance credits", "Accounts receivable", "Accounts payable"):
        assert label in done
    assert _link(done, "/", "Open company")

    # The same upload opened again creates nothing and asks for the file again.
    ui.cookies.set(UPLOAD_COOKIE, upload, path=base)
    r = await ui.post(f"{base}/open", data=fields)
    assert r.status_code == 200
    assert "This upload is no longer available. Choose the file again." in _page(r)
    assert _link(_page(r), base, "Upload the file again")
    assert await _companies(real_engine) == before + 1


async def test_open_page_shows_why_a_file_is_refused(ui, real_engine):
    user = await _owner(real_engine)
    alpha = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    ui.cookies.set("celerp_token", await _token(real_engine, user, alpha))
    before = await _companies(real_engine)
    r = await ui.post("/setup/new-company/open-copy/read",
                      files={"file": ("backup.tar.gz", b"\x1f\x8b\x08rest", "application/octet-stream")})
    page = _page(r)
    assert "This is a full Celerp backup. Use Restore a Celerp backup instead." in page
    assert 'type="file"' in r.text
    assert await _companies(real_engine) == before


async def test_make_page_bad_run_id_shows_plain_message(ui, real_engine):
    user = await _owner(real_engine)
    alpha = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    ui.cookies.set("celerp_token", await _token(real_engine, user, alpha))
    r = await ui.get("/company-copy?from_run=not-a-uuid")
    page = _page(r)
    assert "Invalid value" in page
    assert "uuid_parsing" not in page and "'loc'" not in page


async def test_copy_pages_are_owner_only(ui, real_engine):
    from celerp.models.accounting import UserCompany
    user = await _owner(real_engine)
    alpha = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    clerk = await _owner(real_engine, "clerk@example.com")
    async with maker(real_engine)() as s:
        s.add(UserCompany(user_id=clerk, company_id=alpha, role="viewer"))
        await s.commit()
    ui.cookies.set("celerp_token", await _token(real_engine, clerk, alpha, role="viewer"))
    for method, path in (("get", "/company-copy"), ("post", "/company-copy"),
                         ("get", "/setup/new-company/open-copy")):
        r = await getattr(ui, method)(path)
        assert r.status_code == 403, path
    r = await ui.get("/company-copy/abc/download")
    assert r.status_code == 403


async def test_fresh_installation_opens_copy(ui, real_engine, code_config):
    user = await _owner(real_engine)
    alpha = await _company(real_engine, user, "Alpha Trading", "alpha-marker")
    data = await _make_file(ui, real_engine, user, alpha)
    async with real_engine.begin() as conn:
        await conn.execute(text("TRUNCATE users, companies RESTART IDENTITY CASCADE"))
    ui.cookies.clear()

    r = await ui.get("/setup")
    assert _link(_page(r), "/setup/open-copy", "Open a company copy")
    base = "/setup/open-copy"
    r = await ui.get(base)
    assert 'name="setup_code"' in r.text
    r = await ui.post(f"{base}/read", files={"file": ("alpha.celerp-company", data, "application/octet-stream")},
                      data={"setup_code": code_config})
    assert r.status_code == 200, r.text
    preview = _page(r)
    assert "Alpha Trading" in preview and 'name="password"' in preview and 'name="setup_code"' in preview
    ui.cookies.set(UPLOAD_COOKIE, _cookie(r, UPLOAD_COOKIE), path=base)
    fields = _hidden(preview)

    account = {"name": "Owner", "email": "owner@example.com", "password": "ownerpw123",
               "confirm_password": "different1", "setup_code": code_config}
    r = await ui.post(f"{base}/open", data={**fields, **account})
    assert r.status_code == 200
    assert "Alpha Trading" in _page(r) and 'name="password"' in r.text
    assert await _companies(real_engine) == 0

    r = await ui.post(f"{base}/open", data={**fields, **account, "confirm_password": "ownerpw123"})
    assert r.status_code == 303, r.text
    assert await _companies(real_engine) == 1
    assert await count(real_engine, "users") == 1
    ui.cookies.set("celerp_token", _cookie(r, "celerp_token"))
    r = await ui.get(f"{base}/done")
    assert "Your company is ready." in _page(r) and "Alpha Trading" in _page(r)
