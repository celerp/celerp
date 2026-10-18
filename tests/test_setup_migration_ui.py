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
from ui.i18n import t


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
        "size_bytes": 20480, "source_schema_version": "7",
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
        self.draft_view: dict | None = None   # an export set the API keeps as a draft
        self.scan_bodies: list[bytes] = []
        self.export_requests: list[dict] = []  # newest first, as the API lists them
        self.export_sends: list[dict] = []
        self.export_adds: list[tuple[str, dict]] = []
        self.export_refusal: tuple[int, str] | None = None
        self.move_requests: list[dict] = []    # newest first, as the API lists them
        self.move_sends: list[dict] = []
        self.move_refusal: tuple[int, str] | None = None
        self.xero_fetches: dict[str, dict] = {}   # run -> its Xero attachment fetch, as the API shows it
        self.xero_refusal: tuple[int, str] | None = None
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
            self.scan_bodies.append(body)
            if self.scan_error:
                return _json(422, {"detail": "Celerp cannot read this file yet."})
            if self.expired and _form_field(body, "draft"):
                return _json(410, {"detail": "This scan has expired. Upload the file again."})
            source = _form_field(body, "source") or "manager_io"
            self._n += 1
            if self.draft_view is not None and (_form_field(body, "check") or not self.draft_view["complete"]):
                return _json(200, {"draft_token": f"drafttoken{self._n:04d}secretvalue", "checklist": self.draft_view})
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
        if path.startswith("/migrations/move-requests"):
            return self._move_request(method, path, json.loads(body) if body else {})
        if path.startswith("/migrations/export-requests"):
            return self._export_request(method, path, json.loads(body) if body else {})
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
        if tail.startswith("/xero-attachments"):
            return self._xero_attachments(method, run_id, tail)
        return _json(405, {"detail": "Method not allowed"})

    def _export_request(self, method: str, path: str, data: dict) -> httpx.Response:
        if method == "GET" and path == "/migrations/export-requests":
            return _json(200, {"items": self.export_requests})
        if self.export_refusal is not None:
            return _json(self.export_refusal[0], {"detail": self.export_refusal[1]})
        if method == "POST" and path == "/migrations/export-requests":
            self.export_sends.append(data)
            row = _export_request(client_name=data["client_name"], source=data["source"])
            self.export_requests.insert(0, row)
            return _json(201, {k: row[k] for k in ("id", "link_id", "status")})
        m = re.fullmatch(r"/migrations/export-requests/([0-9a-f-]{36})/(add|revoke)", path)
        row = next((r for r in self.export_requests if m and r["id"] == m.group(1)), None)
        if row is None:
            return _json(404, {"detail": "Export request not found."})
        if m.group(2) == "revoke":
            row.update(status="revoked", readable=False)
            return _json(200, {"status": "revoked"})
        self.export_adds.append((row["id"], data))
        self._n += 1
        return _json(200, {"draft_token": f"drafttoken{self._n:04d}secretvalue", "checklist": self.draft_view})


    def _xero_attachments(self, method: str, run_id: str, tail: str) -> httpx.Response:
        fetch = self.xero_fetches.setdefault(run_id, _xero_fetch("none"))
        if method == "GET":
            return _json(200, fetch)
        if self.xero_refusal is not None:
            return _json(self.xero_refusal[0], {"detail": self.xero_refusal[1]})
        if tail.endswith("/stop"):
            fetch.update(status="stopped")
            fetch.pop("authorize_url", None)
        elif fetch["status"] not in ("fetching", "waiting"):
            fetch.update(_xero_fetch("signing_in"))
        return _json(200, fetch)

    def _move_request(self, method: str, path: str, data: dict) -> httpx.Response:
        if method == "GET" and path == "/migrations/move-requests":
            return _json(200, {"items": self.move_requests})
        if self.move_refusal is not None:
            return _json(self.move_refusal[0], {"detail": self.move_refusal[1]})
        if method == "POST" and path == "/migrations/move-requests":
            self.move_sends.append(data)
            row = _move_request(client_name=data["client_name"])
            self.move_requests.insert(0, row)
            return _json(201, {"id": row["id"], "status": row["status"]})
        m = re.fullmatch(r"/migrations/move-requests/([0-9a-f-]{36})/withdraw", path)
        row = next((r for r in self.move_requests if m and r["id"] == m.group(1)), None)
        if row is None:
            return _json(404, {"detail": "Move request not found."})
        row.update(status="withdrawn", stored=False)
        return _json(200, {"status": "withdrawn"})


def _xero_fetch(status: str, **kw) -> dict:
    """A Xero attachment fetch as the API shows it."""
    view = {"status": status, "organisation": "", "error": "", "attached": 0, "total": 0, "unmatched": [],
            "resume_at": None, **kw}
    if status == "signing_in":
        view.setdefault("authorize_url", "https://login.xero.com/identity/connect/authorize?state=abc")
    return view


def _move_request(**kw) -> dict:
    """One move request as the API lists it."""
    return {"id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()), "firm_name": "Example Accounting",
            "client_name": "Client Ltd", "source": "xero", "status": "received", "note": "",
            "created_at": "2026-10-17T08:00:00+00:00", "submitted_at": "2026-10-17T08:01:00+00:00",
            "updated_at": None, "stored": True,
            "files": [{"id": uuid.uuid4().hex, "name": "General Ledger.txt", "size": 20,
                       "received_at": "2026-10-17T08:00:30+00:00"}], **kw}


def _export_request(**kw) -> dict:
    """One export request as the API lists it."""
    return {"id": str(uuid.uuid4()), "link_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
            "firm_name": "Example Accounting", "client_name": "Client Ltd", "source": "xero",
            "items": [{"label": "General Ledger", "menu": "Accounting > Reports > General Ledger > Export",
                       "range": "From the first transaction to the last"}],
            "status": "sent", "created_at": "2026-10-17T08:00:00+00:00",
            "expires_at": "2026-10-31T08:00:00+00:00", "files": [], "stored": True, "readable": True, **kw}


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
    assert _link(page, "/setup/restore-backup", "Open a company copy")
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
    assert all(set(s) == {"key", "display_name", "accepts", "artifacts", "checklist"} for s in body)
    assert {s["key"]: s["checklist"] is not None for s in body} == {
        a.key: hasattr(a, "checklist") for a in list_adapters()}

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


DRAFT_COOKIE = "celerp_migration_draft"


def _export_set_sources(monkeypatch) -> dict:
    """The fake API offering Xero's export set beside the one-file sources; its blank checklist."""
    from celerp.importers.adapters.exports import XeroAdapter
    blank = XeroAdapter().checklist().view()
    monkeypatch.setattr(sys.modules[__name__], "_SOURCES", [*_SOURCES, {
        "key": "xero", "display_name": "Xero", "accepts": "",
        "artifacts": [{"key": "export_set", "label": "Xero export files",
                       "extensions": [".csv", ".txt", ".xlsx", ".zip"], "max_bytes": 1024 ** 3}],
        "checklist": blank}])
    return blank


async def test_an_export_set_is_checked_file_by_file_and_kept_as_a_draft(ui, router, fake_api, monkeypatch):
    blank = _export_set_sources(monkeypatch)
    r = await ui.get("/setup/migrate?source=xero")
    page = _page(r)
    assert "Files to export from Xero" in page and "Xero closes access" in page
    assert "Accounting > Reports > General Ledger > Export" in page
    assert _link(page, "/setup/migrate/checklist?source=xero", "Open this list to print")
    files = _inputs(page, "files")[0]
    assert _attr(files, "hx-post") == "/setup/migrate/check" and _attr(files, "hx-include") == "closest form"
    assert re.search(r"\bmultiple\b", files)       # an export set is many files of one kind
    radio = next(tag for tag in _inputs(page, "source") if _attr(tag, "value") == "xero")
    assert _attr(radio, "hx-get") == "/setup/migrate/checklist?source=xero"

    printable = _page(await ui.get("/setup/migrate/checklist?source=xero"))
    assert "Files to export from Xero" in printable and 'onclick="window.print()"' in printable
    assert "is missing" not in printable    # a blank list names no problems before the first file
    one_file = _page(await ui.get("/setup/migrate/checklist?source=manager_io"))
    assert 'id="export-checklist"' in one_file and "has no list of files to export" in one_file

    # A one-file source is never checked as a set: its chosen file stays in the field.
    r = await ui.post("/setup/migrate/check", files=_UPLOAD, data={"source": "manager_io"})
    assert r.status_code == 200 and fake_api.scan_bodies == []
    assert "X-Export-Draft-Kept" not in r.headers

    fake_api.draft_view = {**blank, "problems": ["General Ledger is missing. Export it from Accounting > "
                                                 "Reports > General Ledger > Export."],
                           "uploads": [{"name": "notes.txt", "stored_name": "artifact-1"}]}
    r = await ui.post("/setup/migrate/check", files={"files": ("notes.txt", b"call the bank", "text/plain")},
                      data={"source": "xero"})
    assert r.status_code == 200 and r.headers["X-Export-Draft-Kept"] == "1"
    assert _form_field(fake_api.scan_bodies[-1], "check") == "1"
    token = _cookie_value(r, DRAFT_COOKIE)
    assert "HttpOnly" in _cookie_header(r, DRAFT_COOKIE)
    panel = _page(r)
    assert panel.startswith('<div id="export-checklist"') and "General Ledger is missing" in panel
    assert '"remove": "artifact-1"' in panel and "Remove" in panel
    assert "Choose it again to put it back" in panel

    ui.cookies.set(DRAFT_COOKIE, token, path="/setup/migrate")
    r = await ui.post("/setup/migrate/check", data={"source": "xero", "remove": "artifact-1"},
                      files={"files": ("", b"", "application/octet-stream")})
    body = fake_api.scan_bodies[-1]
    assert _form_field(body, "draft") == token and _form_field(body, "remove") == "artifact-1"

    # Analyze with the set still short: the page says so and keeps every received file.
    r = await ui.post("/setup/migrate/scan", data={"source": "xero"},
                      files={"files": ("", b"", "application/octet-stream")})
    assert r.status_code == 200
    page = _page(r)
    assert "The set is not ready to analyze yet" in page and "General Ledger is missing" in page
    # Said inside the list, so the next check that swaps the list in also clears it.
    assert page.index('id="export-checklist"') < page.index("The set is not ready to analyze yet")
    assert _form_field(fake_api.scan_bodies[-1], "draft") == token
    assert _form_field(fake_api.scan_bodies[-1], "check") is None
    ui.cookies.set(DRAFT_COOKIE, _cookie_value(r, DRAFT_COOKIE), path="/setup/migrate")

    fake_api.draft_view = {**blank, "complete": True, "problems": [], "uploads": []}
    r = await ui.post("/setup/migrate/scan", data={"source": "xero"},
                      files={"files": ("", b"", "application/octet-stream")})
    assert r.status_code == 303 and r.headers["location"] == "/setup/migrate/coverage"
    assert _cookie_value(r, SCAN_COOKIE) in fake_api.scans and _cleared(r, DRAFT_COOKIE)


async def test_an_expired_draft_is_dropped_with_the_reason(ui, router, fake_api, monkeypatch):
    blank = _export_set_sources(monkeypatch)
    fake_api.draft_view = {**blank, "uploads": []}
    fake_api.expired = True
    ui.cookies.set(DRAFT_COOKIE, "drafttoken0001secretvalue", path="/setup/migrate")
    r = await ui.post("/setup/migrate/check", data={"source": "xero"},
                      files={"files": ("Contacts.csv", b"*ContactName\r\n", "text/csv")})
    assert r.status_code == 200 and "This scan has expired" in _page(r)
    assert _cleared(r, DRAFT_COOKIE) and "X-Export-Draft-Kept" not in r.headers
    assert "Files to export from Xero" in _page(r)    # the blank list stays to start again


# ---------------------------------------------------------------------------
# Asking a client for their export files
# ---------------------------------------------------------------------------

REQUESTS = "/setup/new-company/migrate/requests"


async def test_a_firm_emails_a_client_the_checklist_and_adds_what_arrives(ui, router, fake_api, monkeypatch):
    blank = _export_set_sources(monkeypatch)
    router.overrides[("GET", "/company-backups/sender")] = _respond(
        200, {"firm_name": "Example Accounting", "reply_to": "firm@example.com"})
    _owner(ui)
    page = _page(await ui.get("/setup/new-company/migrate"))
    section = re.search(r'<div[^>]*id="export-requests"[^>]*>', page).group(0)
    assert _attr(section, "hx-get") == REQUESTS and _attr(section, "hx-trigger") == "load"
    assert _attr(section, "hx-swap") == "outerHTML"  # the panel replaces the placeholder, never nests in it
    assert 'id="export-requests"' not in _page(await ui.get("/setup/migrate"))   # no firm before setup

    r = await ui.get(REQUESTS)
    panel = _page(r)
    assert panel.startswith('<div id="export-requests"')
    assert "Ask a client for their export files" in panel and "only this installation can read" in panel
    assert "No export requests yet." in panel
    assert _attr(_inputs(panel, "firm_name")[0], "value") == "Example Accounting"
    assert _attr(_inputs(panel, "reply_to")[0], "value") == "firm@example.com"
    operation = _attr(_inputs(panel, "operation_id")[0], "value")
    assert str(uuid.UUID(operation)) == operation
    # Only a source with a list of files to export can be asked for.
    assert re.findall(r'<option value="([^"]*)"', panel) == ["xero"]
    form = re.search(r'<form[^>]*id="export-request-form"[^>]*>', panel).group(0)
    assert _attr(form, "hx-post") == REQUESTS and _attr(form, "hx-target") == "#export-requests"

    typed = {"operation_id": operation, "client_name": "Client Ltd", "recipient": "client@example.com",
             "source": "xero", "firm_name": "Example Accounting", "reply_to": "firm@example.com"}
    fake_api.export_refusal = (502, "The email could not be sent. Try again")
    failed = _page(await ui.post(REQUESTS, data=typed))
    assert "The email could not be sent" in failed
    assert _attr(_inputs(failed, "operation_id")[0], "value") == operation        # a retry is the same send
    assert _attr(_inputs(failed, "client_name")[0], "value") == "Client Ltd"
    fake_api.export_refusal = (428, "Sign in to a free Celerp account to email an export checklist.")
    needed = _page(await ui.post(REQUESTS, data=typed))
    assert 'id="celerp-account-panel"' in needed and "export-send" in needed
    fake_api.export_refusal = None

    sent = _page(await ui.post(REQUESTS, data=typed))
    assert fake_api.export_sends == [typed]
    assert f"Export checklist sent to {typed['recipient']}." in sent
    assert _attr(_inputs(sent, "operation_id")[0], "value") != operation          # the next send is new
    assert "Waiting for exports" in sent and "Client Ltd" in sent and "No export requests yet." not in sent

    older = _export_request(client_name="Older Client", created_at="2026-10-01T08:00:00+00:00")
    arrived = _export_request(client_name="Second Client",
                              files=[{"id": str(uuid.uuid4()), "name": "Contacts.csv", "size": 20,
                                      "received_at": "2026-10-17T09:00:00+00:00"}])
    fake_api.export_requests = [arrived, *fake_api.export_requests, older]
    panel = _page(await ui.get(REQUESTS))
    assert panel.index("Second Client") < panel.index("Client Ltd") < panel.index("Older Client")   # newest first
    assert "Contacts.csv" in panel and "cannot be undone" in panel
    add = f"{REQUESTS}/{arrived['id']}/add"
    assert re.search(rf'<form[^>]*action="{re.escape(add)}"', panel)
    assert f'action="{REQUESTS}/{older["id"]}/add"' not in panel                  # nothing arrived to add

    fake_api.draft_view = {**blank, "uploads": [{"name": "Contacts.csv", "stored_name": "artifact-1"}]}
    ui.cookies.set(DRAFT_COOKIE, "drafttoken0001secretvalue", path="/setup/new-company/migrate")
    r = await ui.post(add)
    assert r.status_code == 303 and r.headers["location"] == "/setup/new-company/migrate?source=xero&added=1"
    assert fake_api.export_adds == [(arrived["id"], {"draft_token": "drafttoken0001secretvalue"})]
    token = _cookie_value(r, DRAFT_COOKIE)
    ui.cookies.set(DRAFT_COOKIE, token, path="/setup/new-company/migrate")
    added = await ui.get(r.headers["location"])
    page = _page(added)
    assert "Contacts.csv" in page and '"remove": "artifact-1"' in page        # the files are in the upload
    assert t("migration.export_request_added") in _visible(added)             # and the page says so
    assert t("migration.export_request_added") not in _page(await ui.get("/setup/new-company/migrate?source=xero"))
    assert _form_field(fake_api.scan_bodies[-1], "draft") == token

    revoked = _page(await ui.post(f"{REQUESTS}/{older['id']}/revoke"))
    assert revoked.startswith('<div id="export-requests"') and older["status"] == "revoked"
    assert "Revoked" in revoked


async def test_only_the_owner_asks_for_exports_and_failures_are_said(ui, router, fake_api, monkeypatch):
    _export_set_sources(monkeypatch)
    for role in ("admin", "viewer"):
        _owner(ui, role)
        router.calls.clear()
        for r in (await ui.get(REQUESTS), await ui.post(REQUESTS, data={"client_name": "x"})):
            assert "Only the company owner can move a company into Celerp." in _page(r), role
        assert router.migration_calls() == [], role

    _owner(ui)
    fake_api.export_requests = [_export_request(files=[{"id": str(uuid.uuid4()), "name": "Contacts.csv",
                                                        "size": 20, "received_at": "2026-10-17T09:00:00+00:00"}])]
    fake_api.export_refusal = (409, "This installation does not hold the key for these files.")
    r = await ui.post(f"{REQUESTS}/{fake_api.export_requests[0]['id']}/add")
    assert r.status_code == 200 and "does not hold the key" in _page(r)
    router.overrides[("GET", "/migrations/export-requests")] = _respond(502, {"detail": "Cloud unreachable"})
    assert "Cloud unreachable" in _page(await ui.get(REQUESTS))


# ---------------------------------------------------------------------------
# Asking Celerp's team to move a client
# ---------------------------------------------------------------------------

MOVES = "/setup/new-company/migrate/move-requests"


async def test_a_firm_asks_celerp_to_move_a_client_from_the_open_export_set(ui, router, fake_api, monkeypatch):
    _export_set_sources(monkeypatch)
    router.overrides[("GET", "/company-backups/sender")] = _respond(
        200, {"firm_name": "Example Accounting", "reply_to": "firm@example.com"})
    _owner(ui)
    page = _page(await ui.get("/setup/new-company/migrate"))
    section = re.search(r'<div[^>]*id="move-requests"[^>]*>', page).group(0)
    assert _attr(section, "hx-get") == MOVES and _attr(section, "hx-trigger") == "load"
    assert _attr(section, "hx-swap") == "outerHTML"  # the panel replaces the placeholder, never nests in it
    assert 'id="move-requests"' not in _page(await ui.get("/setup/migrate"))      # no firm before setup

    panel = _page(await ui.get(MOVES))
    assert panel.startswith('<div id="move-requests"')
    assert "Ask Celerp to move this client" in panel
    # What happens to the files is said before anything is sent.
    assert "Celerp's team reads these files" in panel and "30 days" in panel
    assert "No move requests yet." in panel
    assert _attr(_inputs(panel, "firm_name")[0], "value") == "Example Accounting"
    assert _attr(_inputs(panel, "reply_to")[0], "value") == "firm@example.com"
    operation = _attr(_inputs(panel, "operation_id")[0], "value")
    assert str(uuid.UUID(operation)) == operation
    form = re.search(r'<form[^>]*id="move-request-form"[^>]*>', panel).group(0)
    assert _attr(form, "hx-post") == MOVES and _attr(form, "hx-target") == "#move-requests"

    typed = {"operation_id": operation, "client_name": "Client Ltd", "firm_name": "Example Accounting",
             "reply_to": "firm@example.com"}
    fake_api.move_refusal = (422, "Add the export files on this page first, then ask us to move this client.")
    failed = _page(await ui.post(MOVES, data=typed))
    assert "Add the export files on this page first" in failed
    assert _attr(_inputs(failed, "operation_id")[0], "value") == operation      # a retry is the same send
    assert _attr(_inputs(failed, "client_name")[0], "value") == "Client Ltd"
    fake_api.move_refusal = (428, "Sign in to a free Celerp account to ask us to move a client.")
    needed = _page(await ui.post(MOVES, data=typed))
    assert 'id="celerp-account-panel"' in needed and "move-send" in needed
    fake_api.move_refusal = None

    ui.cookies.set(DRAFT_COOKIE, "drafttoken0001secretvalue", path="/setup/new-company/migrate")
    sent = _page(await ui.post(MOVES, data=typed))
    assert fake_api.move_sends == [{**typed, "draft_token": "drafttoken0001secretvalue"}]
    assert f"Sent to the Celerp team. We will reply to {typed['reply_to']}." in sent
    assert _attr(_inputs(sent, "operation_id")[0], "value") != operation        # the next send is new

    older = _move_request(client_name="Older Client", status="declined", note="The ledger does not balance.",
                          created_at="2026-10-01T08:00:00+00:00", stored=False)
    working = _move_request(client_name="Second Client", status="in_progress", note="Started on the 2023 books.",
                            created_at="2026-10-17T09:00:00+00:00")
    fake_api.move_requests = [working, *fake_api.move_requests, older]
    panel = _page(await ui.get(MOVES))
    assert panel.index("Second Client") < panel.index("Client Ltd") < panel.index("Older Client")   # newest first
    assert "In progress" in panel and "Declined" in panel and "Received" in panel
    assert "Started on the 2023 books." in panel and "The ledger does not balance." in panel
    assert "General Ledger.txt" in panel and "cannot be undone" in panel
    withdraw = f"{MOVES}/{working['id']}/withdraw"
    assert f'hx-post="{withdraw}"' in panel
    assert f'hx-post="{MOVES}/{older["id"]}/withdraw"' not in panel              # a closed request stays

    withdrawn = _page(await ui.post(withdraw))
    assert withdrawn.startswith('<div id="move-requests"') and working["status"] == "withdrawn"
    assert "Move request withdrawn. Its files were deleted." in withdrawn


async def test_only_the_owner_asks_celerp_to_move_a_client(ui, router, fake_api, monkeypatch):
    _export_set_sources(monkeypatch)
    for role in ("admin", "viewer"):
        _owner(ui, role)
        router.calls.clear()
        for r in (await ui.get(MOVES), await ui.post(MOVES, data={"client_name": "x"}),
                  await ui.post(f"{MOVES}/{uuid.uuid4()}/withdraw")):
            assert "Only the company owner can move a company into Celerp." in _page(r), role
        assert router.migration_calls() == [], role

    _owner(ui)
    fake_api.move_requests = [_move_request(status="returned", stored=False)]
    fake_api.move_refusal = (409, "This request is returned, so it cannot be withdrawn")
    r = await ui.post(f"{MOVES}/{fake_api.move_requests[0]['id']}/withdraw")
    assert r.status_code == 200 and "cannot be withdrawn" in _page(r)
    assert "Move request not found." in _page(await ui.post(f"{MOVES}/not-a-uuid/withdraw"))
    router.overrides[("GET", "/migrations/move-requests")] = _respond(502, {"detail": "Cloud unreachable"})
    assert "Cloud unreachable" in _page(await ui.get(MOVES))


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
    assert 'hx-trigger="every 2s"' in _page(r)
    # Once started, the footnote no longer counts down to a start, and stopping reads as a pause.
    assert "if you do not start" not in page and "the migration finishes or is discarded" in page
    assert re.search(rf'action="/migrations/{run_id}/cancel"[^>]*>\s*<button[^>]*>Pause import</button>', _page(r))

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
    assert _link(complete, f"/company-backup/download?from_run={run_id}", "Download company copy")
    order = [complete.find(text) for text in ("Open company", "Send company copy", "Invite client",
                                              "Move another company")]
    assert -1 not in order and order == sorted(order)
    assert _link(complete, f"/switch-company/{company_id}?next=/settings/users/new", "Invite client")
    assert "they sign in to your Celerp and see only this company." in complete
    assert "they get their own Celerp with this company in it." in complete
    assert "Record a correcting journal entry" not in complete

    r = await ui.get(f"/migrations/{run_id}/pack")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("check,source,celerp")

    for text in (coverage, verify, complete):
        assert "Manager" not in text
    for text in (coverage, verify):
        assert re.search(r"\bclient", text, re.I) is None

    sample_id = fake_api.add_run("completed", company_name="Sample company", is_sample=True)
    r = await ui.get(f"/migrations/{sample_id}/complete")
    assert r.status_code == 200
    page = _visible(r)
    assert "That's the whole migration." in page
    assert _link(page, "/setup/new-company/migrate", "Move your first company")
    assert _link(page, f"/switch-company/{fake_api.runs[sample_id]['company_id']}", "Open")
    assert "Download company copy" not in page


_FAILED_ROW = {"check": "ar_control", "key": "", "currency": "USD", "source": "3120.00",
               "celerp": "3020.00", "difference": "-100.00", "rule": "exact", "result": "fail"}


async def test_differences_are_shown_once_with_both_ways_forward(ui, router, fake_api):
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize")
    fake_api.recon[run_id]["rows"][1] = dict(_FAILED_ROW)
    fake_api.recon[run_id]["blockers"] = 1
    r = await ui.get(f"/migrations/{run_id}/verify")
    assert r.status_code == 200
    page = _visible(r)
    assert page.count("$-100.00") == 1
    assert "Finish now and correct later with a journal entry." in page
    assert "Or fix the file in Manager.io and upload it again." in page
    assert ("Finishing cannot be undone: the company goes live and new records can be built on it. "
            "Before finishing you can still go back or discard the migration.") in page
    box = re.search(r'<input\b[^>]*name="reviewed_differences"[^>]*>', page)
    assert box and "required" not in box.group(0)
    assert re.search(rf'<form\b[^>]*action="/migrations/{run_id}/start"[^>]*>.*?Check again.*?</form>',
                     page, re.S)

    r = await ui.post(f"/migrations/{run_id}/finalize", data={"reviewed_differences": "true"})
    assert r.status_code == 303
    assert fake_api.finalized_with[-1]["reviewed_differences"] is True
    fake_api.recon[run_id]["reviewed_differences"] = {"by": "Owner", "at": "2026-10-16T08:00:00+00:00"}
    r = await ui.get(f"/migrations/{run_id}/complete")
    complete = _visible(r)
    company_id = fake_api.runs[run_id]["company_id"]
    assert _link(complete, f"/switch-company/{company_id}?next=/accounting/journal/new",
                 "Record a correcting journal entry")
    assert complete.find("Open company") < complete.find("Record a correcting journal entry") \
        < complete.find("Send company copy")


async def test_check_again_reruns_the_verification(ui, router, fake_api):
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize")
    r = await ui.post(f"/migrations/{run_id}/start")
    assert r.status_code == 303 and r.headers["location"] == f"/migrations/{run_id}"
    assert fake_api.runs[run_id]["status"] == "running"
    assert "Check again" in t("migration.err_verification_stale", "en")
    assert "Resume" not in t("migration.err_verification_stale", "en")


def test_wizard_step_bar_is_styled():
    from pathlib import Path
    css = (Path(__file__).parents[1] / "ui" / "static" / "app.css").read_text()
    for cls in ("wizard-steps", "wizard-step", "step-num", "step-num--active", "step-num--done",
                "step-label", "step-label--active"):
        assert re.search(rf"\.{re.escape(cls)}\s*[{{:]", css), cls


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
async def test_verify_page_says_where_to_add_an_account_and_sets_discard_apart(ui, fake_api):
    """The hint names the choice that adds an account, and Discard reads as the
    destructive action it is, not as a link like the download."""
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize")
    fake_api.posting[run_id] = [_posting_row("general_expense", "General expenses", proposal=_EXPENSE_PROPOSAL)]
    html = _page(await ui.get(f"/migrations/{run_id}/verify"))
    assert "Posting accounts" in html and "add the one shown" not in html
    assert "choose Add account in its list" in html
    assert re.search(rf'href="/migrations/{run_id}/discard" class="btn btn--outline btn--danger', html)


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

    fake_api.runs[run_id]["status"] = "completed"
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
    # Every posting account picker is searchable, however short the chart is today.
    for role in ("payable", "general_expense", "sales_revenue"):
        assert re.search(rf'class="combobox-wrap">\s*<input[^>]*>\s*<input type="hidden" name="role.{role}"',
                         verify), role
    assert "<select" not in verify
    # The source's single control account is offered already chosen.
    assert re.search(r'value="210 Creditors"[^>]*>\s*<input type="hidden" name="role.payable"[^>]*value="210"',
                     verify)
    # Where the chart has nothing suitable, adding the proposed account is offered.
    assert re.search(r'data-value="__new__"[^>]*>Add account 6950 General expenses \(Expense\)</div>', verify)
    # A role no workflow needs yet is not asked for.
    assert "Exchange gain" not in verify

    fake_api.refuse_finalize = "Choose the posting account for: Sales revenue."
    r = await ui.post(f"/migrations/{run_id}/finalize",
                      data={"role.payable": "211", "role.general_expense": "__new__", "role.sales_revenue": ""})
    assert r.status_code == 200
    page = _visible(r)
    assert "Choose the posting account for: Sales revenue." in page
    assert re.search(r'name="role.payable"[^>]*value="211"', page)
    assert fake_api.runs[run_id]["status"] == "ready_to_finalize"

    fake_api.refuse_finalize = ""
    r = await ui.post(f"/migrations/{run_id}/finalize",
                      data={"role.payable": "211", "role.general_expense": "__new__", "role.sales_revenue": "405"})
    assert r.status_code == 303 and r.headers["location"] == f"/migrations/{run_id}/complete"
    assert fake_api.finalized_with == [{
        "roles": {"payable": "211", "sales_revenue": "405"},
        "add_accounts": [{**_EXPENSE_PROPOSAL, "role": "general_expense"}],
        "reviewed_differences": False,
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
    assert re.search(r'value="Creditors"[^>]*>\s*<input type="hidden"[^>]*value="M0a1b2c3d-1"', verify)
    assert re.search(r'data-value="211"[^>]*>211 Other creditors</div>', verify)
    assert re.search(r'data-value="Mdeadbeef"[^>]*>Mdeadbeef Bank charges</div>', verify)
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
    staged = t("auth.company_staged", "en")
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
async def test_the_polled_progress_drops_still_running_once_ready(ui, fake_api):
    """The line saying the move keeps running is part of what the page re-reads, so it
    goes when the run is ready; the attachments count reads as a plain number."""
    _owner(ui)
    run_id = fake_api.add_run("running")
    hx = {"HX-Request": "true"}
    assert "The migration keeps running" in (await ui.get(f"/migrations/{run_id}", headers=hx)).text
    fake_api.runs[run_id]["status"] = "ready_to_finalize"
    fake_api.runs[run_id]["phases"][-1].update(phase="attachments", created=1)
    html = (await ui.get(f"/migrations/{run_id}", headers=hx)).text
    assert "The migration keeps running" not in html
    assert "1 attachments" not in html and '<td class="cell--number">1</td>' in html


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
    assert "(20 KB)" in text                                  # the file's size, not a hash of it
    assert "sales_invoices" not in _page(r) and "sales invoices" not in text


_HTMX = {"HX-Request": "true"}


async def test_the_files_attached_in_xero_are_fetched_from_the_check_results_page(ui, router, fake_api, monkeypatch):
    _export_set_sources(monkeypatch)
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize", source="xero")
    base = f"/migrations/{run_id}/xero-attachments"
    for page_path in (f"/migrations/{run_id}/verify", f"/migrations/{run_id}/complete"):
        if page_path.endswith("complete"):
            fake_api.runs[run_id]["status"] = "completed"
        section = re.search(r'<div[^>]*id="xero-attachments"[^>]*>', _page(await ui.get(page_path))).group(0)
        assert _attr(section, "hx-get") == base and _attr(section, "hx-trigger") == "load", page_path
        assert _attr(section, "hx-swap") == "outerHTML"
    # Only a migration from Xero offers it, and never the sample company.
    other = fake_api.add_run("ready_to_finalize")
    assert 'id="xero-attachments"' not in _page(await ui.get(f"/migrations/{other}/verify"))
    sample = fake_api.add_run("completed", source="xero", is_sample=True)
    assert 'id="xero-attachments"' not in _page(await ui.get(f"/migrations/{sample}/complete"))

    panel = _page(await ui.get(base, headers=_HTMX))
    assert panel.startswith('<div id="xero-attachments"')
    assert "Attachments in Xero" in panel and "only reads" in panel
    form = re.search(r'<form[^>]*id="xero-attachments-form"[^>]*>', panel).group(0)
    assert _attr(form, "hx-post") == base and _attr(form, "hx-target") == "#xero-attachments"
    assert "hx-trigger=\"every" not in panel               # nothing to watch before a fetch starts

    fake_api.xero_refusal = (428, "Sign in to a free Celerp account to fetch attachments from Xero.")
    needed = _page(await ui.post(base))
    assert 'id="celerp-account-panel"' in needed and "xero-attachments" in needed
    fake_api.xero_refusal = None

    signing = _page(await ui.post(base))
    url = "https://login.xero.com/identity/connect/authorize?state=abc"
    link = re.search(r'<a[^>]*>Sign in to Xero</a>', signing).group(0)
    assert _attr(link, "href") == url and _attr(link, "target") == "_blank"
    assert f'data-copy-text="{url}"' in signing             # the link can go to whoever holds the login
    assert "choose this client's organisation" in signing
    assert f'hx-post="{base}/stop"' in signing
    assert _attr(re.search(r'<div[^>]*id="xero-attachments"[^>]*>', signing).group(0), "hx-trigger") == "every 5s"

    fake_api.xero_fetches[run_id] = _xero_fetch("fetching", organisation="Demo Trading Ltd", attached=3, total=10)
    fetching = _page(await ui.get(base, headers=_HTMX))
    assert "Demo Trading Ltd" in fetching and "3 of 10 files attached so far" in fetching
    assert "every 5s" in fetching and f'hx-post="{base}/stop"' in fetching

    fake_api.xero_fetches[run_id] = _xero_fetch(
        "waiting", organisation="Demo Trading Ltd", attached=3, total=10, resume_at="2026-10-17T10:00:00+00:00",
        error="Xero's daily limit was reached. Fetching continues when it resets.")
    waiting = _page(await ui.get(base, headers=_HTMX))
    assert "daily limit was reached" in waiting and "every 5s" in waiting

    fake_api.xero_fetches[run_id] = _xero_fetch(
        "done", organisation="Demo Trading Ltd", attached=4, total=5, unmatched=[
            {"document": "INV-9999", "contact": "Gone Customer", "files": 1,
             "reason": "This document was not imported into Celerp."}])
    done = _visible(await ui.get(base, headers=_HTMX))
    assert "4 of 5 files from Demo Trading Ltd were attached" in done
    assert "INV-9999" in done and "Gone Customer" in done and "This document was not imported into Celerp." in done
    done_page = _page(await ui.get(base, headers=_HTMX))
    assert "every 5s" not in done_page and "Fetch again" in done_page
    files_header = re.search(r'<th[^>]*>Files</th>', done_page).group(0)
    assert "cell--number" in files_header                    # a count sits right, under its header

    fake_api.xero_fetches[run_id] = _xero_fetch("failed", error="Choose exactly one organisation when you sign in to Xero.")
    failed = _page(await ui.get(base, headers=_HTMX))
    assert "exactly one organisation" in failed and "Try again" in failed

    fake_api.xero_fetches[run_id] = _xero_fetch("fetching", attached=1, total=2)
    stopped = _page(await ui.post(f"{base}/stop"))
    assert stopped.startswith('<div id="xero-attachments"') and "Fetching stopped" in stopped
    assert "Fetch again" in stopped


async def test_the_xero_attachments_address_opened_directly_goes_to_its_run(ui, fake_api, monkeypatch):
    _export_set_sources(monkeypatch)
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize", source="xero")
    r = await ui.get(f"/migrations/{run_id}/xero-attachments")
    assert r.status_code == 303 and r.headers["location"] == f"/migrations/{run_id}"


async def test_the_finished_page_of_an_unfinished_run_goes_to_the_run(ui, fake_api):
    _owner(ui)
    run_id = fake_api.add_run("ready_to_finalize")
    r = await ui.get(f"/migrations/{run_id}/complete")
    assert r.status_code == 303 and r.headers["location"] == f"/migrations/{run_id}"


async def test_a_xero_import_says_attachments_are_fetched_after_it(ui, fake_api, monkeypatch):
    _export_set_sources(monkeypatch)
    r = await ui.post("/setup/migrate/scan", files=_UPLOAD, data={"source": "manager_io"})
    token = _cookie_value(r, SCAN_COOKIE)
    ui.cookies.set(SCAN_COOKIE, token)
    hint = "fetch them from Xero on the check results page"
    assert hint not in _visible(await ui.get("/setup/migrate/coverage"))
    fake_api.scans[token]["scan"].update(source_system="xero", display_name="Xero")
    assert hint in _visible(await ui.get("/setup/migrate/coverage"))


@pytest.mark.asyncio
async def test_finish_pressed_again_shows_the_run_where_it_is(ui, fake_api):
    """A second Finish (double click, a stale tab) on an import already on its way is
    refused on the run's own page, which keeps itself current, in plain words."""
    _owner(ui)
    run_id = fake_api.add_run("running")
    r = await ui.post(f"/migrations/{run_id}/finalize")
    assert r.status_code == 200
    page = _visible(r)
    assert "The migration keeps running" in page and "Finish migration" not in page
    for action in ("start", "cancel", "finalize"):
        text = t(f"migration.err_cannot_{action}", "en", status=t("migration.status.running", "en"))
        assert '"' not in text and "Refresh" not in text, text
