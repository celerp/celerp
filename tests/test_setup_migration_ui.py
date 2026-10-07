# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Setup choice screens and the company migration wizard (UI side).

The UI talks to the API only through ui.api_client. These tests replace the one
client factory with a router transport: migration routes go to an in-test fake
that follows the frozen API route contract, everything else goes to the real API
app on the test database, so registration, restore and bootstrap status are real.
"""

from __future__ import annotations

import copy
import html as _html
import json
import re
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from celerp.models.migration import MigrationStatus
from test_helpers import make_test_token


# ---------------------------------------------------------------------------
# Fake migration API (frozen route contract)
# ---------------------------------------------------------------------------

SCAN_COOKIE = "celerp_migration_scan"

_SOURCES = [
    {"key": "manager_io", "display_name": "Manager.io",
     "artifacts": [{"key": "backup", "label": "Backup file", "extensions": [".manager"],
                    "max_bytes": 2 * 1024 ** 3}]},
    {"key": "ledgerly", "display_name": "Ledgerly Books",
     "artifacts": [{"key": "export", "label": "Export file", "extensions": [".ldg"],
                    "max_bytes": 1024 ** 3}]},
]

_ACCOUNT_OPTIONS = [f"{4100 + i} Income account {i}" for i in range(12)]

_PHASE_LABELS = [
    "Company and settings", "Accounting masters", "Contacts and locations", "Inventory",
    "Documents", "Settlements", "Inventory openings",
    "Residual journals and opening balances", "Attachments", "Reconciliation",
    "Ready to finish",
]
_PHASES = [
    "company_settings", "currencies_taxes_accounts", "contacts_locations",
    "inventory_masters", "operational_documents", "settlements",
    "inventory_opening_adjustments", "residual_journals_or_cutover_opening",
    "attachments", "reconciliation", "ready_to_finalize",
]


def _scan_view(source: str, file_name: str, *, suggested: str = _ACCOUNT_OPTIONS[0]) -> dict:
    display = next(s["display_name"] for s in _SOURCES if s["key"] == source)
    return {
        "source_system": source, "display_name": display, "file_name": file_name,
        "size_bytes": 20480, "sha256_short": "9f2c41aa", "source_schema_version": "7",
        "company_name": "Harbor Goods Ltd", "base_currency": "USD",
        "period_start": "2024-01-01", "period_end": "2025-12-31",
        "currencies": ["USD", "EUR"],
        "object_counts": {"accounts": 42, "customers": 12, "suppliers": 0, "sales_invoices": 310},
        "features": ["multi_currency"],
        "coverage": [
            {"source_type": "Customer", "count": 12, "coverage_class": "mapped",
             "target": "contact", "note": None},
            {"source_type": "Sales invoice", "count": 310, "coverage_class": "mapped",
             "target": "document", "note": None},
            {"source_type": "Audit log", "count": 400, "coverage_class": "ignored_non_business",
             "target": None, "note": None},
            {"source_type": "Payslip", "count": 3, "coverage_class": "unsupported_nonfinancial",
             "target": None, "note": "Payslips are not imported."},
        ],
        "blockers": [],
        "warnings": ["Payslips are not imported."],
        "questions": [{"key": "account:fx-gain", "source_type": "Account",
                       "source_external_id": "acc-9", "label": "Exchange gain account",
                       "options": list(_ACCOUNT_OPTIONS), "suggested": suggested}],
        "decisions": None,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


def _run_view(run_id: str, *, status: str, source: str = "manager_io",
              company_name: str = "Harbor Goods Ltd", counters: int = 0,
              is_sample: bool = False) -> dict:
    phases = []
    for i, (phase, label) in enumerate(zip(_PHASES, _PHASE_LABELS)):
        done = status in ("ready_to_finalize", "completed") or i < 3
        phases.append({
            "phase": phase, "label": label,
            "status": "done" if done else ("running" if i == 3 and status == "running" else "pending"),
            "created": 0 if phase == "attachments" else counters + i,
            "skipped": 1 if i == 2 else 0, "errors": 0,
        })
    return {
        "id": run_id, "company_id": str(uuid.uuid4()), "company_name": company_name,
        "source_system": source, "mode": "full_history", "cutover_date": None,
        "status": status, "current_phase": "inventory_masters", "phases": phases,
        "coverage": [], "error": None, "preparing": False, "retention_until": None,
        "source_deleted": False, "prepared_by": "Example Bookkeeping",
        "is_bootstrap_run": False, "is_sample": is_sample,
    }


_RECON = {
    "generated_at": "2026-09-29T10:00:00+00:00",
    "blockers": 0,
    "rows": [
        {"check": "debits_equal_credits", "key": "", "currency": "USD", "source": "48210.55",
         "celerp": "48210.55", "difference": "0.00", "rule": "exact", "result": "pass"},
        {"check": "ar_control", "key": "", "currency": "USD", "source": "3120.00",
         "celerp": "3120.00", "difference": "0.00", "rule": "exact", "result": "pass"},
        {"check": "ap_control", "key": "", "currency": "USD", "source": "1875.40",
         "celerp": "1875.40", "difference": "0.00", "rule": "exact", "result": "pass"},
    ],
}


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


def _form_field(body: bytes, name: str) -> str | None:
    m = re.search(rb'name="' + name.encode() + rb'"\r\n\r\n(.*?)\r\n--', body, re.S)
    return m.group(1).decode() if m else None


def _file_name(body: bytes) -> str | None:
    m = re.search(rb'filename="([^"]*)"', body)
    return m.group(1).decode() if m else None


class FakeMigrationAPI:
    """Implements the migration API route contract in memory."""

    def __init__(self) -> None:
        self.scans: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.recon: dict[str, dict] = {}
        self.posting: dict[str, list[dict]] = {}  # run -> the posting accounts it needs
        self.finalized_with: list[dict] = []
        self.refuse_finalize = ""
        self.decisions_posted: list[dict] = []
        self.started: dict[str, str] = {}  # scan token -> the run a company-mode start created
        self.scan_error = False
        self.expired = False
        self.with_questions = True
        self._n = 0

    def add_run(self, status: str, **kw) -> str:
        run_id = str(uuid.uuid4())
        self.runs[run_id] = _run_view(run_id, status=status, **kw)
        self.recon[run_id] = copy.deepcopy(_RECON)
        return run_id

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        body = request.content
        method, path = request.method, request.url.path
        if method == "GET" and path == "/migrations/sources":
            return _json(200, _SOURCES)
        if method == "POST" and path in ("/migrations/bootstrap/scan", "/migrations/scan"):
            if self.scan_error:
                return _json(422, {"detail": "Celerp cannot read this file yet."})
            source = _form_field(body, "source") or "manager_io"
            self._n += 1
            token = f"scantoken{self._n:04d}secretvalue"
            scan = _scan_view(source, _file_name(body) or "upload")
            if not self.with_questions:
                scan["questions"] = []
            self.scans[token] = {"scan": scan}
            return _json(200, {"scan_token": token, "scan": self.scans[token]["scan"]})
        if method == "POST" and path in ("/migrations/bootstrap/scan/read", "/migrations/scan/read"):
            data = json.loads(body)
            if path == "/migrations/scan/read" and data.get("scan_token") in self.started:
                return _json(200, {"run_id": self.started[data["scan_token"]]})
            if self.expired or data.get("scan_token") not in self.scans:
                return _json(410, {"detail": "This scan has expired. Upload the file again."})
            return _json(200, {"scan": self.scans[data["scan_token"]]["scan"]})
        if method == "POST" and path in ("/migrations/bootstrap/decisions", "/migrations/scan/decisions"):
            data = json.loads(body)
            if self.expired or data.get("scan_token") not in self.scans:
                return _json(410, {"detail": "This scan has expired. Upload the file again."})
            self.decisions_posted.append(data)
            scan = self.scans[data["scan_token"]]["scan"]
            scan["decisions"] = {k: data.get(k) for k in ("mode", "cutover_date", "mappings", "prepared_by")}
            return _json(200, {"scan": scan})
        if method == "POST" and path in ("/migrations/bootstrap/start", "/migrations/start-from-scan"):
            data = json.loads(body)
            if path == "/migrations/start-from-scan" and data.get("scan_token") in self.started:
                return _json(200, {"run_id": self.started[data["scan_token"]]})
            if data.get("scan_token") not in self.scans:
                return _json(410, {"detail": "This scan has expired. Upload the file again."})
            run_id = self.add_run("running", company_name=data.get("company_name") or "")
            if path == "/migrations/start-from-scan":
                # The API moves the scan's files into the run: the scan itself is gone.
                del self.scans[data["scan_token"]]
                self.started[data["scan_token"]] = run_id
                return _json(201, {"run_id": run_id})
            return _json(201, {"access_token": make_test_token("owner"),
                               "refresh_token": "refresh-new", "run_id": run_id})
        m = re.fullmatch(r"/migrations/([0-9a-f-]{36})(/[a-z/-]+)?", path)
        if not m or m.group(1) not in self.runs:
            return _json(404, {"detail": "Migration not found."})
        run_id, tail = m.group(1), m.group(2) or ""
        run = self.runs[run_id]
        if method == "GET" and tail == "":
            return _json(200, run)
        if method == "GET" and tail == "/posting-accounts":
            return _json(200, {"roles": self.posting.get(run_id, [])})
        if method == "GET" and tail == "/reconciliation":
            return _json(200, self.recon[run_id])
        if method == "GET" and tail == "/reconciliation/pack":
            return httpx.Response(
                200, content=b"check,source,celerp\r\ndebits_equal_credits,48210.55,48210.55\r\n",
                headers={"content-type": "text/csv",
                         "content-disposition": f'attachment; filename="reconciliation-{run_id}.csv"'})
        if method == "POST" and tail == "/start":
            run["status"] = "running"
            return _json(202, run)
        if method == "POST" and tail == "/cancel":
            run["status"] = "cancel_requested"
            return _json(202, run)
        if method == "POST" and tail == "/finalize":
            if run["status"] != "ready_to_finalize":
                return _json(409, {"detail": f"Cannot finalize a migration that is {run['status']}."})
            if self.refuse_finalize:
                return _json(409, {"detail": self.refuse_finalize})
            self.finalized_with.append(json.loads(body) if body else {})
            run["status"] = "completed"
            return _json(200, run)
        if method == "POST" and tail == "/discard":
            del self.runs[run_id]
            return _json(200, {"redirect": "/"})
        return _json(405, {"detail": "Method not allowed"})


class Router(httpx.AsyncBaseTransport):
    """Routes UI-originated API calls: overrides, then the fake, then the real API."""

    def __init__(self, fake: FakeMigrationAPI) -> None:
        import celerp.main
        self.fake = fake
        self.overrides: dict[tuple[str, str], object] = {}
        self.calls: list[tuple[str, str, str]] = []
        self._real = httpx.ASGITransport(app=celerp.main.app)

    def migration_calls(self) -> list[tuple[str, str, str]]:
        return [c for c in self.calls if c[2].startswith("/migrations")]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, request.url.host, path))
        hook = self.overrides.get((method, path))
        if hook is not None:
            return hook(request)
        if path.startswith("/migrations"):
            return await self.fake.handle(request)
        return await self._real.handle_async_request(request)


@pytest.fixture
def fake_api() -> FakeMigrationAPI:
    return FakeMigrationAPI()


@pytest.fixture
def router(fake_api, client, monkeypatch) -> Router:
    """Depends on `client` so the real API app runs on the rolled-back test session."""
    import ui.api_client as api
    r = Router(fake_api)

    def _factory(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url="http://api", headers=merged, transport=r,
                                 follow_redirects=follow_redirects, timeout=timeout)

    monkeypatch.setattr(api, "_local_client", _factory)
    return r


@pytest.fixture
def sample_module(tmp_path, monkeypatch):
    """The sample artifact module the wizard uploads, pointed at a small file."""
    artifact = tmp_path / "sample.manager"
    artifact.write_bytes(b"sample source file")
    mod = types.ModuleType("celerp.importers.sample")
    mod.SAMPLE_ARTIFACT = artifact
    mod.SAMPLE_COMPANY_NAME = "Sample company"
    monkeypatch.setitem(sys.modules, "celerp.importers.sample", mod)
    return mod


@pytest_asyncio.fixture
async def ui(router):
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://localhost", follow_redirects=False) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _page(r: httpx.Response) -> str:
    return _html.unescape(r.text)


def _visible(r: httpx.Response) -> str:
    """Page text with script blocks removed."""
    return re.sub(r"<script\b.*?</script>", "", _page(r), flags=re.S | re.I)


def _link(page: str, href: str, text: str = "") -> bool:
    pat = rf'<a\b[^>]*href="{re.escape(href)}"[^>]*>[^<]*{re.escape(text)}[^<]*</a>'
    return re.search(pat, page) is not None


def _back(page: str, href: str) -> bool:
    return _link(page, href, "Back")


def _inputs(page: str, name: str) -> list[str]:
    return [tag for tag in re.findall(r"<input\b[^>]*>", page) if f'name="{name}"' in tag]


def _attr(tag: str, attr: str) -> str | None:
    m = re.search(rf'\b{attr}="([^"]*)"', tag)
    return m.group(1) if m else None


def _checked_value(page: str, name: str) -> str | None:
    for tag in _inputs(page, name):
        if re.search(r"\bchecked\b", tag):
            return _attr(tag, "value")
    return None


def _set_cookies(r: httpx.Response) -> list[str]:
    return r.headers.get_list("set-cookie")


def _cookie_header(r: httpx.Response, name: str) -> str | None:
    return next((c for c in _set_cookies(r) if c.startswith(f"{name}=")), None)


def _cookie_value(r: httpx.Response, name: str) -> str:
    header = _cookie_header(r, name)
    assert header is not None, f"no {name} cookie set"
    return header.split(";", 1)[0].split("=", 1)[1].strip('"')


def _cleared(r: httpx.Response, name: str) -> bool:
    header = _cookie_header(r, name)
    return header is not None and ("Max-Age=0" in header or f'{name}="";' in header or f"{name}=;" in header)


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def _users_companies(session) -> tuple[int, int]:
    from celerp.models.company import Company, User
    return await _count(session, User), await _count(session, Company)


async def _migration_runs(session) -> int:
    from celerp.models.migration import MigrationRun
    return await _count(session, MigrationRun)


def _raise(exc_type):
    def hook(request: httpx.Request):
        raise exc_type("simulated", request=request)
    return hook


def _respond(status: int, body: dict):
    return lambda request: httpx.Response(status, json=body)


def _owner(ui: httpx.AsyncClient, role: str = "owner") -> None:
    ui.cookies.set("celerp_token", make_test_token(role))


_UPLOAD = {"files": ("books.manager", b"source bytes", "application/octet-stream")}


# ---------------------------------------------------------------------------
# /setup choices (unbootstrapped)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_form_offers_restore_and_move(ui, router, session, sample_module):
    r = await ui.get("/setup")
    assert r.status_code == 200
    page = _page(r)
    assert re.search(r'<form\b[^>]*action="/setup"', page)
    assert _link(page, "/setup/restore-backup", "Restore a Celerp backup")
    assert _link(page, "/setup/migrate", "Move your books from another system")

    for path in ("/setup/restore-backup", "/setup/migrate"):
        r = await ui.get(path)
        assert r.status_code == 200, path
        assert _back(_page(r), "/setup"), path
    # Recovering a whole installation is offered from the restore page.
    assert _link(_page(await ui.get("/setup/restore-backup")), "/setup/import-backup",
                 "Recover an entire Celerp installation")
    r = await ui.get("/setup/import-backup")
    assert r.status_code == 200
    assert _back(_page(r), "/setup")

    r = await ui.post("/setup/migrate/sample")
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/migrate/coverage"
    ui.cookies.set(SCAN_COOKIE, _cookie_value(r, SCAN_COOKIE))
    r = await ui.get("/setup/migrate/coverage")
    assert r.status_code == 200
    assert _back(_page(r), "/setup/migrate")

    assert await _users_companies(session) == (0, 0)
    assert await _migration_runs(session) == 0

    router.overrides[("GET", "/auth/bootstrap-status")] = _respond(
        200, {"bootstrapped": True, "setup_code_required": False})
    for path in ("/setup", "/setup/migrate"):
        r = await ui.get(path)
        assert r.status_code == 302, path
        assert r.headers["location"] == "/login"


_GOOD_SETUP = {"company_name": "Keep Co", "name": "Kept Name", "email": "kept@example.com",
               "password": "correct-horse-9", "confirm_password": "correct-horse-9",
               "vertical": "blank", "currency": "USD", "timezone": "UTC"}


@pytest.mark.asyncio
async def test_setup_registers_and_applies_without_migration_state(ui, router, session):
    r = await ui.post("/setup", data={**_GOOD_SETUP, "company_name": "Fresh Start Ltd",
                                      "email": "owner@example.com"})
    assert r.status_code == 302
    assert r.headers["location"] in ("/dashboard", "/setup/activating")
    assert _cookie_header(r, "celerp_token") and _cookie_header(r, "celerp_refresh")
    assert await _users_companies(session) == (1, 1)
    assert await _migration_runs(session) == 0
    assert router.migration_calls() == []


@pytest.mark.asyncio
async def test_setup_failure_states_keep_typed_values(ui, router, session):
    from ui.i18n import t

    router.overrides[("GET", "/auth/bootstrap-status")] = _raise(httpx.ConnectError)
    r = await ui.get("/setup")
    assert r.status_code == 200
    assert t("error.api_unavailable") in _page(r)
    del router.overrides[("GET", "/auth/bootstrap-status")]

    good = _GOOD_SETUP
    cases = [
        ({**good, "name": ""}, t("settings.all_fields_required"), None),
        ({**good, "confirm_password": "different-horse-9"}, t("settings.passwords_do_not_match"), None),
        ({**good, "password": "short", "confirm_password": "short"}, t("settings.password_min_length"), None),
        (good, t("auth.setup_code_required"),
         (("GET", "/auth/bootstrap-status"),
          _respond(200, {"bootstrapped": False, "setup_code_required": True}))),
        (good, "That email address cannot be registered.",
         (("POST", "/auth/register"),
          _respond(400, {"detail": "That email address cannot be registered."}))),
    ]
    for form, message, override in cases:
        router.overrides.clear()
        if override:
            router.overrides[override[0]] = override[1]
        r = await ui.post("/setup", data=form)
        assert r.status_code == 200, message
        page = _page(r)
        assert message in page
        assert _attr(_inputs(page, "company_name")[0], "value") == form["company_name"]
        assert _attr(_inputs(page, "email")[0], "value") == form["email"]
        assert _attr(_inputs(page, "name")[0], "value") == form["name"]
    router.overrides.clear()

    assert await _users_companies(session) == (0, 0)
    assert await _migration_runs(session) == 0


@pytest.mark.asyncio
async def test_setup_password_mismatch_shown_at_confirm_field(ui, router, session):
    """A mismatch marks the confirm field and says so under it, not at the top of the form."""
    from ui.i18n import t

    r = await ui.post("/setup", data={**_GOOD_SETUP, "confirm_password": "different-horse-9"})
    assert r.status_code == 200
    page = _page(r)
    confirm = _inputs(page, "confirm_password")[0]
    assert _attr(confirm, "aria-invalid") == "true"
    assert _attr(confirm, "aria-describedby") == "confirm_password-error"
    assert _attr(_inputs(page, "password")[0], "aria-invalid") is None
    msg = re.search(r'<p\b[^>]*id="confirm_password-error"[^>]*>(.*?)</p>', page, re.S)
    assert msg and msg.group(1).strip() == t("settings.passwords_do_not_match")
    assert "flash--error" not in page, "the message sits at the field, not in a banner"
    assert page.index('name="confirm_password"') < page.index('id="confirm_password-error"')
    assert await _users_companies(session) == (0, 0)


@pytest.mark.asyncio
async def test_setup_restore_errors_rerender_form_with_back(ui, router, session):
    from ui.i18n import t

    upload = lambda content: {"backup_file": ("backup.tar.gz", content, "application/gzip")}  # noqa: E731

    r = await ui.post("/setup/import-backup", files=upload(b""))
    assert r.status_code == 200
    assert t("auth.file_empty") in _page(r)
    assert _back(_page(r), "/setup")

    router.overrides[("POST", "/backup/import-bootstrap")] = _respond(
        400, {"detail": "This backup was made by a newer Celerp version."})
    r = await ui.post("/setup/import-backup", files=upload(b"backup bytes"))
    assert r.status_code == 200
    assert "This backup was made by a newer Celerp version." in _page(r)
    assert _back(_page(r), "/setup")

    router.overrides[("POST", "/backup/import-bootstrap")] = _raise(httpx.ReadTimeout)
    r = await ui.post("/setup/import-backup", files=upload(b"backup bytes"))
    assert r.status_code == 200
    assert t("auth.import_timed_out") in _page(r)
    assert _back(_page(r), "/setup")

    assert await _users_companies(session) == (0, 0)


# ---------------------------------------------------------------------------
# /setup/new-company choices (existing user)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_existing_new_company_landing_offers_fresh_or_migrate(ui, router, fake_api, session, monkeypatch):
    _owner(ui)
    r = await ui.get("/setup/new-company")
    assert r.status_code == 200
    page = _page(r)
    assert _link(page, "/setup/new-company/fresh", "Start fresh")
    assert _link(page, "/setup/new-company/migrate", "Move from another system")
    assert _back(page, "/settings/general?tab=company")

    r = await ui.get("/setup/new-company/fresh")
    assert r.status_code == 200
    page = _page(r)
    assert _inputs(page, "company_name")
    action = re.search(r'<form\b[^>]*action="([^"]+)"', page).group(1)
    assert _back(page, "/setup/new-company")

    from unittest.mock import AsyncMock
    monkeypatch.setattr("ui.api_client.create_company",
                        AsyncMock(return_value=("access-new", "refresh-new")))
    r = await ui.post(action, data={"company_name": "Second Co"})
    assert r.status_code == 302
    assert r.headers["location"] == "/setup/company"
    assert _cookie_value(r, "celerp_token") == "access-new"
    assert _cookie_value(r, "celerp_refresh") == "refresh-new"
    r = await ui.post(action, data={"company_name": "  "})
    assert r.status_code in (302, 303)
    assert "error" in r.headers["location"]

    ui.cookies.clear()  # the create response seated the stubbed new-company tokens
    _owner(ui)
    r = await ui.get("/setup/new-company/migrate")
    assert r.status_code == 200
    assert _back(_page(r), "/setup/new-company")

    refusal = "Only the company owner can move a company into Celerp."
    for role in ("admin", "manager", "operator", "viewer"):
        router.calls.clear()
        _owner(ui, role)
        r = await ui.get("/setup/new-company")
        assert _link(_page(r), "/setup/new-company/migrate", "Move from another system"), role
        r = await ui.get("/setup/new-company/migrate")
        assert refusal in _page(r), role
        assert _back(_page(r), "/setup/new-company"), role
        r = await ui.post("/setup/new-company/migrate/scan", files=_UPLOAD, data={"source": "manager_io"})
        assert refusal in _page(r), role
        r = await ui.post("/setup/new-company/migrate/sample")
        assert refusal in _page(r), role
        assert router.migration_calls() == [], role
    assert fake_api.runs == {}
    assert fake_api.scans == {}
    assert await _migration_runs(session) == 0


# ---------------------------------------------------------------------------
# Source selection, upload and source change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_selection_offers_source_request_link(ui, router, fake_api, client):
    r = await ui.get("/setup/migrate")
    assert r.status_code == 200
    page = _page(r)
    offered = {_attr(tag, "value") for tag in _inputs(page, "source") if _attr(tag, "type") == "radio"}
    assert {s["key"] for s in _SOURCES} <= offered
    assert offered - {s["key"] for s in _SOURCES} == {""}  # the detect-automatically choice
    for s in _SOURCES:
        assert s["display_name"] in page
    assert "QuickBooks" not in page and "Xero" not in page
    assert "/settings/connectors" not in page
    assert "Don't see your system? Tell us what you use" in page
    form = re.search(r'<form\b[^>]*action="https://celerp.com/migrate/request"[^>]*>(.*?)</form>', page, re.S)
    assert form and re.search(r'method="get"', form.group(0), re.I)
    assert _inputs(form.group(1), "source")
    assert "Your file stays on this Celerp server." in page

    r = await client.get("/migrations/sources")
    assert r.status_code == 200
    from celerp.importers.adapters.registry import list_adapters
    body = r.json()
    assert [s["key"] for s in body] == [a.key for a in list_adapters()]
    assert all(set(s) == {"key", "display_name", "artifacts"} for s in body)

    r = await ui.post("/setup/migrate/scan", files=_UPLOAD,
                      data={"source": "manager_io", "prepared_by": "Example Bookkeeping"})
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/migrate/coverage"
    header = _cookie_header(r, SCAN_COOKIE)
    token = _cookie_value(r, SCAN_COOKIE)
    assert token in fake_api.scans
    assert "HttpOnly" in header and re.search(r"SameSite=strict", header, re.I)
    assert "Path=/setup/migrate" in header
    assert token not in r.headers["location"]
    ui.cookies.set(SCAN_COOKIE, token)

    r = await ui.get("/setup/migrate?source=ledgerly")
    assert r.status_code == 200
    page = _page(r)
    assert _cookie_header(r, SCAN_COOKIE) is None
    confirm = re.search(r'<form\b[^>]*action="/setup/migrate"[^>]*>(.*?)</form>', page, re.S)
    assert confirm and _inputs(confirm.group(1), "confirm")
    assert token in fake_api.scans

    r = await ui.get("/setup/migrate?source=ledgerly&confirm=1&prepared_by=Example+Bookkeeping")
    assert r.status_code == 200
    assert _cleared(r, SCAN_COOKIE)
    assert _checked_value(_page(r), "source") == "ledgerly"
    assert _attr(_inputs(_page(r), "prepared_by")[0], "value") == "Example Bookkeeping"
    ui.cookies.delete(SCAN_COOKIE)

    fake_api.scan_error = True
    r = await ui.post("/setup/migrate/scan", files=_UPLOAD, data={"source": "ledgerly"})
    assert r.status_code == 200
    page = _page(r)
    assert "Celerp cannot read this file yet." in page
    form = re.search(r'<form\b[^>]*action="https://celerp.com/migrate/request"[^>]*>(.*?)</form>', page, re.S)
    assert form and _inputs(form.group(1), "source")

    assert {host for _, host, _ in router.calls} == {"api"}


# ---------------------------------------------------------------------------
# Progress, verify, finish
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_progress_survives_process_restart(ui, router, fake_api):
    _owner(ui)
    run_id = fake_api.add_run("running", counters=1200)
    r = await ui.get(f"/migrations/{run_id}")
    assert r.status_code == 200
    page = _visible(r)
    positions = [page.find(label) for label in _PHASE_LABELS]
    assert all(p >= 0 for p in positions), dict(zip(_PHASE_LABELS, positions))
    assert positions == sorted(positions)
    assert "1203" in page  # inventory phase created counter
    assert "0 attachments" in page
    assert 'hx-trigger="every 2s"' in _page(r)

    # The runner process died: the API reports the stale heartbeat as interrupted
    # with the durable counters from the last committed batch.
    fake_api.runs[run_id] = _run_view(run_id, status="interrupted", counters=1500)
    from ui.app import app as ui_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app),
                                 base_url="http://localhost", follow_redirects=False) as fresh:
        _owner(fresh)
        r = await fresh.get(f"/migrations/{run_id}")
        assert r.status_code == 200
        page = _page(r)
        assert "Interrupted - Resume" in page
        assert re.search(rf'<form\b[^>]*action="/migrations/{run_id}/start"', page)
        assert "every 2s" not in page
        assert "1503" in page

        r = await fresh.get(f"/migrations/{run_id}", headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "<html" not in r.text.lower()
        assert "1503" in r.text

        r = await fresh.post(f"/migrations/{run_id}/start")
        assert r.status_code == 303
        assert r.headers["location"] == f"/migrations/{run_id}"
        assert fake_api.runs[run_id]["status"] == "running"


@pytest.mark.asyncio
async def test_finalize_success_state_offers_next_actions(ui, router, fake_api, sample_module):
    _owner(ui)
    r = await ui.post("/setup/new-company/migrate/scan", files=_UPLOAD, data={"source": "manager_io"})
    assert r.status_code == 303
    ui.cookies.set(SCAN_COOKIE, _cookie_value(r, SCAN_COOKIE))
    r = await ui.get("/setup/new-company/migrate/coverage")
    assert r.status_code == 200
    coverage = _visible(r)
    assert "Customer" in coverage

    run_id = fake_api.add_run("ready_to_finalize")
    r = await ui.get(f"/migrations/{run_id}/verify")
    assert r.status_code == 200
    verify = _visible(r)
    for header in ("Check", "Source", "Celerp", "Difference", "Result"):
        assert re.search(rf"<th\b[^>]*>\s*{header}\s*</th>", verify), header
    for header in ("Source", "Celerp", "Difference"):
        assert re.search(rf'<th\b[^>]*class="[^"]*cell--number[^"]*"[^>]*>\s*{header}', verify), header
    assert "$48,210.55" in verify
    assert re.search(rf'<form\b[^>]*action="/migrations/{run_id}/finalize"', verify)
    assert "Finish migration" in verify
    assert _link(verify, f"/migrations/{run_id}/pack", "Download reconciliation pack")

    r = await ui.post(f"/migrations/{run_id}/finalize")
    assert r.status_code == 303
    assert r.headers["location"] == f"/migrations/{run_id}/complete"
    assert fake_api.runs[run_id]["status"] == "completed"

    r = await ui.get(f"/migrations/{run_id}/complete")
    assert r.status_code == 200
    complete = _visible(r)
    assert "Your company is ready." in complete
    for total in ("$48,210.55", "$3,120.00", "$1,875.40"):
        assert total in complete
    assert _link(complete, f"/migrations/{run_id}/pack", "Download reconciliation pack")
    company_id = fake_api.runs[run_id]["company_id"]
    assert _link(complete, f"/switch-company/{company_id}", "Open company")
    assert not _link(complete, "/dashboard")
    assert _link(complete, f"/setup/new-company/migrate?from_run={run_id}", "Move another company")
    assert _link(complete, f"/company-backup/download?from_run={run_id}", "Download company backup")

    r = await ui.get(f"/migrations/{run_id}/pack")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("check,source,celerp")

    for text in (coverage, verify, complete):
        assert "Manager" not in text
        assert re.search(r"\bclient", text, re.I) is None

    sample_id = fake_api.add_run("completed", company_name="Sample company", is_sample=True)
    r = await ui.get(f"/migrations/{sample_id}/complete")
    assert r.status_code == 200
    page = _visible(r)
    assert "That's the whole migration." in page
    assert _link(page, "/setup/new-company/migrate", "Move your first company")
    assert _link(page, f"/switch-company/{fake_api.runs[sample_id]['company_id']}", "Open")
    assert "Download company backup" not in page


_AP_KEY = "5f1c2a90-3b7e-4d21-9a0c-6e8f1d2b3c4a"
_SUPPLIER_KEY = "0b9d8c7e-6f5a-4e3d-8c2b-1a0f9e8d7c6b"
_ITEM_KEY = "7c6b5a4f-3e2d-4c1b-9a0f-8e7d6c5b4a39"
_LABELLED_ROWS = [
    {"check": "ap_control", "key": _AP_KEY, "currency": "USD", "label": "2100 Accounts payable",
     "credit_normal": True, "source": "-1875.4000", "celerp": "-1875.4000", "difference": "0.0000",
     "rule": "exact", "result": "pass"},
    {"check": "ap_by_supplier", "key": _SUPPLIER_KEY, "currency": "USD", "label": "Northwind Supplies",
     "credit_normal": True, "source": "-310.5", "celerp": "-310.5", "difference": "0", "rule": "exact",
     "result": "pass"},
    {"check": "inventory_quantity", "key": _ITEM_KEY, "currency": None, "label": "Blue widget (WID)",
     "credit_normal": False, "source": "12.000", "celerp": "12.000", "difference": "0E-3", "rule": "exact",
     "result": "pass"},
    {"check": "document_status", "key": "bill:awaiting_payment", "currency": None, "label": "",
     "credit_normal": False, "source": "3", "celerp": "3", "difference": "0", "rule": "exact", "result": "pass"},
    {"check": "document_count", "key": "invoice", "currency": "USD", "label": "", "credit_normal": False,
     "source": "14", "celerp": "14", "difference": "0", "rule": "exact", "result": "pass"},
    {"check": "settlement_allocation", "key": "payment", "currency": "USD", "label": "", "credit_normal": False,
     "source": "2200.5", "celerp": "2200.5", "difference": "0", "rule": "exact", "result": "pass"},
]


@pytest.mark.asyncio
async def test_verify_page_names_records_and_formats_figures(ui, router, fake_api):
    """RED before the change: rows showed the source key, raw enums and raw decimals."""
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize")
    fake_api.recon[run_id]["rows"] = copy.deepcopy(_LABELLED_ROWS)
    r = await ui.get(f"/migrations/{run_id}/verify")
    assert r.status_code == 200
    page = _visible(r)
    for text in ("Payables control: 2100 Accounts payable", "Payables by supplier: Northwind Supplies",
                 "Inventory quantity: Blue widget (WID)", "Document status: Bill: Awaiting Payment",
                 "Document count: Invoice (USD)", "Settlement allocation: Payment"):
        assert text in page, text
    # Business-normal sign: a payable balance reads as the amount owed, at currency precision.
    for figure in ("$1,875.40", "$310.50", "$0.00", "$2,200.50"):
        assert f">{figure}<" in page, figure
    assert ">12<" in page and ">14<" in page and ">3<" in page
    for raw in (_AP_KEY, _SUPPLIER_KEY, _ITEM_KEY, "bill:awaiting_payment", "awaiting_payment", "1875.4000",
                "-1,875.40", "0E-3", "12.000"):
        assert raw not in page, raw

    r = await ui.get(f"/migrations/{run_id}/complete")
    assert r.status_code == 200
    assert _AP_KEY not in _visible(r)


def _posting_row(role: str, label: str, *, required: bool = True, current: str | None = None,
                 current_name: str | None = None, preselect: str | None = None, candidates: tuple = (),
                 proposal: dict | None = None, generated: tuple = ()) -> dict:
    """``generated``: the codes an importer made up (code_generated on the account)."""
    def account(code: str, name: str, account_type: str) -> dict:
        return {"code": code, "name": name, "account_type": account_type, "code_generated": code in generated}

    return {"role": role, "label": label, "group": "core", "required": required, "current": current,
            "current_account": account(current, current_name, "asset") if current and current_name else None,
            "controls": [], "preselect": preselect,
            "candidates": [account(c, n, ty) for c, n, ty in candidates],
            "proposal": proposal or {"code": "9999", "name": label, "account_type": "expense"}}


_EXPENSE_PROPOSAL = {"code": "6950", "name": "General expenses", "account_type": "expense"}


@pytest.mark.asyncio
async def test_finishing_asks_for_each_posting_account_the_company_needs(ui, router, fake_api):
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize")
    fake_api.posting[run_id] = [
        _posting_row("receivable", "Accounts receivable", current="120"),
        _posting_row("payable", "Accounts payable", preselect="210",
                     candidates=(("210", "Creditors", "liability"), ("211", "Other creditors", "liability"))),
        _posting_row("general_expense", "General expenses", proposal=_EXPENSE_PROPOSAL),
        _posting_row("sales_revenue", "Sales revenue",
                     candidates=tuple((f"4{n:02d}", f"Sales {n}", "revenue") for n in range(12))),
        _posting_row("fx_gain", "Exchange gain", required=False),
    ]
    verify = _visible(await ui.get(f"/migrations/{run_id}/verify"))
    assert "Posting accounts" in verify
    # An account already set is shown, not asked for again.
    assert re.search(r"<td>Accounts receivable</td>\s*<td>120</td>", verify)
    assert 'name="role.receivable"' not in verify
    # The source's single control account is offered already chosen.
    assert re.search(r'<select[^>]*name="role.payable"[^>]*>.*?<option value="210" selected>210 Creditors</option>',
                     verify, re.S)
    # Where the chart has nothing suitable, adding the proposed account is offered.
    assert re.search(r'<option value="__new__">Add account 6950 General expenses \(Expense\)</option>', verify)
    # More than ten options become a searchable picker.
    assert re.search(r'class="combobox-wrap".*?name="role.sales_revenue"', verify, re.S)
    # A role no workflow needs yet is not asked for.
    assert "Exchange gain" not in verify

    fake_api.refuse_finalize = "Choose the posting account for: Sales revenue."
    r = await ui.post(f"/migrations/{run_id}/finalize",
                      data={"role.payable": "211", "role.general_expense": "__new__", "role.sales_revenue": ""})
    assert r.status_code == 200
    page = _visible(r)
    assert "Choose the posting account for: Sales revenue." in page
    assert re.search(r'<option value="211" selected>', page)
    assert fake_api.runs[run_id]["status"] == "ready_to_finalize"

    fake_api.refuse_finalize = ""
    r = await ui.post(f"/migrations/{run_id}/finalize",
                      data={"role.payable": "211", "role.general_expense": "__new__", "role.sales_revenue": "405"})
    assert r.status_code == 303 and r.headers["location"] == f"/migrations/{run_id}/complete"
    assert fake_api.finalized_with == [{
        "roles": {"payable": "211", "sales_revenue": "405"},
        "add_accounts": [{**_EXPENSE_PROPOSAL, "role": "general_expense"}],
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["en", "de"])
async def test_finishing_names_accounts_the_source_gave_no_code_by_their_name(ui, router, fake_api, lang):
    """Manager books carry accounts without a code; Celerp gives each an internal code
    (M and eight hex digits) and records that it did. The posting accounts show such an
    account by its name alone, while the picker still submits the code. A code someone
    chose is shown, however it is spelled."""
    _owner(ui)
    ui.cookies.set("celerp_lang", lang)
    run_id = fake_api.add_run("ready_to_finalize")
    fake_api.posting[run_id] = [
        _posting_row("receivable", "Accounts receivable", current="M73d6d4fc", current_name="Debtors",
                     generated=("M73d6d4fc",)),
        _posting_row("payable", "Accounts payable", preselect="M0a1b2c3d-1",
                     candidates=(("M0a1b2c3d-1", "Creditors", "liability"), ("211", "Other creditors", "liability"),
                                 ("Mdeadbeef", "Bank charges", "liability")),
                     generated=("M0a1b2c3d-1",)),
    ]
    verify = _visible(await ui.get(f"/migrations/{run_id}/verify"))

    assert re.search(r"<td>Debtors</td>", verify)
    assert re.search(r'<option value="M0a1b2c3d-1" selected>Creditors</option>', verify)
    assert re.search(r'<option value="211">211 Other creditors</option>', verify)
    assert re.search(r'<option value="Mdeadbeef">Mdeadbeef Bank charges</option>', verify)
    shown = re.sub(r"<[^>]*>", " ", verify)
    assert "M73d6d4fc" not in shown and "M0a1b2c3d" not in shown


@pytest.mark.asyncio
async def test_company_mode_start_keeps_the_working_session(ui, router, fake_api):
    """Starting another company's migration never replaces the session cookies."""
    _owner(ui)
    working = ui.cookies.get("celerp_token")
    base = "/setup/new-company/migrate"
    r = await ui.post(f"{base}/scan", files=_UPLOAD, data={"source": "manager_io"})
    ui.cookies.set(SCAN_COOKIE, _cookie_value(r, SCAN_COOKIE))
    router.calls.clear()
    r = await ui.post(f"{base}/start", data={"company_name": "Harbor Goods Ltd"})
    assert r.status_code == 303, r.text
    run_id = r.headers["location"].rsplit("/", 1)[1]
    assert r.headers["location"] == f"/migrations/{run_id}"
    assert _cookie_header(r, "celerp_token") is None
    assert _cookie_header(r, "celerp_refresh") is None
    assert _cleared(r, SCAN_COOKIE)
    assert [c for c in router.migration_calls() if "start" in c[2]] == [("POST", "api", "/migrations/start-from-scan")]
    assert ui.cookies.get("celerp_token") == working


@pytest.mark.asyncio
async def test_bootstrap_start_signs_in_the_new_owner(ui, router, fake_api):
    """With no working session to keep, bootstrap signs the first owner in."""
    r = await ui.post("/setup/migrate/scan", files=_UPLOAD, data={"source": "manager_io"})
    assert r.status_code == 303, r.text
    ui.cookies.set(SCAN_COOKIE, _cookie_value(r, SCAN_COOKIE))
    router.calls.clear()
    r = await ui.post("/setup/migrate/start", data={
        "company_name": "Harbor Goods Ltd", "name": "Owner", "email": "owner@example.com",
        "password": "ownerpw123", "confirm_password": "ownerpw123"})
    assert r.status_code == 303, r.text
    assert _cookie_value(r, "celerp_token")
    assert [c for c in router.migration_calls() if "start" in c[2]] == [("POST", "api", "/migrations/bootstrap/start")]


# ---------------------------------------------------------------------------
# Move another company
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_move_another_company_starts_clean_wizard(ui, router, fake_api):
    _owner(ui)
    base = "/setup/new-company/migrate"

    # The previous company's wizard: its scan got a user-chosen mapping.
    r = await ui.post(f"{base}/scan", files=_UPLOAD,
                      data={"source": "ledgerly", "prepared_by": "Example Bookkeeping"})
    old_token = _cookie_value(r, SCAN_COOKIE)
    ui.cookies.set(SCAN_COOKIE, old_token)
    r = await ui.post(f"{base}/decisions", data={
        "mode": "full_history", "mapping__account:fx-gain": _ACCOUNT_OPTIONS[7]})
    assert r.status_code == 303
    assert fake_api.decisions_posted[-1]["mappings"] == {"account:fx-gain": _ACCOUNT_OPTIONS[7]}

    previous = fake_api.add_run("completed", source="ledgerly")
    before = copy.deepcopy(fake_api.runs[previous])
    router.calls.clear()

    r = await ui.get(f"{base}?from_run={previous}")
    assert r.status_code == 200
    page = _page(r)
    assert _checked_value(page, "source") == "ledgerly"
    radios = {_attr(tag, "value") for tag in _inputs(page, "source") if _attr(tag, "type") == "radio"}
    assert {"", "manager_io", "ledgerly"} <= radios
    assert _attr(_inputs(page, "prepared_by")[0], "value") == "Example Bookkeeping"
    assert _cleared(r, SCAN_COOKIE)
    assert _back(page, f"/migrations/{previous}/complete")
    assert _ACCOUNT_OPTIONS[7] not in page
    assert not any("scan" in path or "decisions" in path for _, _, path in router.calls)
    assert fake_api.runs[previous] == before
    ui.cookies.delete(SCAN_COOKIE)

    r = await ui.post(f"{base}/scan", files=_UPLOAD,
                      data={"source": "manager_io", "prepared_by": "Example Bookkeeping"})
    assert r.status_code == 303
    new_token = _cookie_value(r, SCAN_COOKIE)
    assert new_token != old_token
    ui.cookies.set(SCAN_COOKIE, new_token)
    r = await ui.post(f"{base}/decisions", data={"mode": "full_history"})
    assert r.status_code == 303
    assert r.headers["location"] == f"{base}/mapping"
    posted = fake_api.decisions_posted[-1]
    assert posted["scan_token"] == new_token
    assert posted["mappings"] == {"account:fx-gain": _ACCOUNT_OPTIONS[0]}
    assert posted["prepared_by"] == "Example Bookkeeping"
    assert fake_api.runs[previous] == before


@pytest.mark.asyncio
async def test_staged_session_lands_on_its_migration_run(ui, router, fake_api):
    """A first owner whose start response was lost signs in to the staged company;
    every landing page sends them to that run instead of ERP pages the API refuses."""
    _owner(ui)
    run_id = fake_api.add_run("preparing")
    staged = "This company is still being moved into Celerp. Finish or discard the migration first."
    router.overrides[("GET", "/companies/me")] = lambda request: _json(403, {"detail": staged})
    router.overrides[("GET", "/migrations/staged")] = lambda request: _json(200, fake_api.runs[run_id])
    for path in ("/", "/login"):
        r = await ui.get(path)
        assert r.status_code == 302 and r.headers["location"] == f"/migrations/{run_id}", path

    r = await ui.get(f"/migrations/{run_id}")
    assert r.status_code == 200
    assert "Preparing" in _visible(r)
    assert 'hx-trigger="every 2s"' in _page(r)


@pytest.mark.asyncio
async def test_company_mode_lost_start_response_returns_to_the_same_run(ui, router, fake_api):
    """The start's response never arrived, so the browser still holds the scan: every
    wizard step and a repeated start lead back to the run it created, never a second one."""
    _owner(ui)
    base = "/setup/new-company/migrate"
    r = await ui.post(f"{base}/scan", files=_UPLOAD, data={"source": "manager_io"})
    scan_token = _cookie_value(r, SCAN_COOKIE)
    ui.cookies.set(SCAN_COOKIE, scan_token)
    r = await ui.post(f"{base}/start", data={"company_name": "Harbor Goods Ltd"})
    assert r.status_code == 303, r.text
    run_url = r.headers["location"]
    ui.cookies.set(SCAN_COOKIE, scan_token)

    for step in ("/coverage", "/mapping", "/review"):
        r = await ui.get(f"{base}{step}")
        assert r.status_code == 303 and r.headers["location"] == run_url, (step, r.status_code)
    r = await ui.post(f"{base}/start", data={"company_name": "Harbor Goods Ltd"})
    assert r.status_code == 303 and r.headers["location"] == run_url, r.text
    assert len(fake_api.runs) == 1


@pytest.mark.parametrize("status", [s.value for s in MigrationStatus])
def test_cancel_offered_only_where_the_run_can_be_cancelled(status):
    """RED before the change: Cancel shows while reconciling, where the run cannot be
    cancelled, so pressing it only returns an error."""
    from fasthtml.common import to_xml

    from celerp.models.migration import can_transition
    from ui.routes.migrations import _run_actions

    shown = "/cancel" in to_xml(_run_actions({"id": "r1", "status": status}))
    assert shown == can_transition(MigrationStatus(status), MigrationStatus.CANCEL_REQUESTED)



@pytest.mark.asyncio
async def test_preparing_run_explains_and_waits_without_a_start_button(ui, fake_api):
    """While Celerp prepares the features a migration needs, the page says so, keeps
    checking, and offers no Start that would only be refused."""
    _owner(ui)
    run_id = fake_api.add_run("ready")
    fake_api.runs[run_id]["preparing"] = True
    r = await ui.get(f"/migrations/{run_id}")
    assert r.status_code == 200
    assert "Celerp is preparing features for this company" in _visible(r)
    assert 'hx-trigger="every 2s"' in _page(r)
    assert f'action="/migrations/{run_id}/start"' not in _page(r)


@pytest.mark.asyncio
async def test_failed_run_shows_plain_message_and_never_says_still_running(ui, fake_api):
    _owner(ui)
    run_id = fake_api.add_run("failed")
    fake_api.runs[run_id]["error"] = {"message": "The migration stopped unexpectedly.",
                                      "missing": ["INV-0042"], "phase": "Invoices"}
    r = await ui.get(f"/migrations/{run_id}")
    text = _visible(r)
    assert "The migration stopped unexpectedly." in text and "INV-0042" in text
    assert "The migration keeps running" not in text
    assert 'hx-trigger="every 2s"' not in _page(r)


@pytest.mark.asyncio
async def test_review_shows_company_currency_scope_and_plain_outcomes(ui, fake_api):
    """The last step before starting names the file, company, currency and dates, and says
    in plain words what will move, using the source's own record names."""
    r = await ui.post("/setup/migrate/scan", files=_UPLOAD, data={"source": "manager_io"})
    token = _cookie_value(r, SCAN_COOKIE)
    ui.cookies.set(SCAN_COOKIE, token)
    fake_api.scans[token]["scan"]["decisions"] = {"mode": "full_history", "cutover_date": None, "mappings": {}}
    r = await ui.get("/setup/migrate/review")
    assert r.status_code == 200, r.text
    text = _visible(r)
    for expected in ("Harbor Goods Ltd", "USD", "2024-01-01", "Sales invoice", "Will move", "Will not move"):
        assert expected in text, expected
    assert "sales_invoices" not in _page(r) and "sales invoices" not in text
