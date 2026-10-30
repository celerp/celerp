# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser proofs for the company migration wizard.

The first-run journey needs a system with no users, while the shared browser
session is always bootstrapped, so it boots its own API and UI servers on a
separate database. The existing-owner journey runs on the shared servers with an
isolated company. The mapping proof routes only the migration API calls to the
in-test fake so the mapping step always has a large option list.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from playwright.sync_api import expect

from boot_support import Supervised, wait_until_started

pytestmark = pytest.mark.browser

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUN_TIMEOUT_MS = 180_000


# ---------------------------------------------------------------------------
# First-run servers (own database, no users)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"port {port} did not open within {timeout}s")


def _pg_admin(url: str):
    import psycopg2
    parts = urlsplit(url.replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=parts.path.lstrip("/"))
    conn.autocommit = True
    return conn


@pytest.fixture
def first_run_ui(tmp_path):
    """API and UI servers in subprocesses on a fresh, empty database and data directory.
    A shared data directory would let two first-run servers replace each other's
    bootstrap scan, which has one owner per install."""
    base_url = os.environ["DATABASE_URL"]
    parts = urlsplit(base_url)
    db_name = f"{parts.path.lstrip('/')}_firstrun"
    db_url = urlunsplit(parts._replace(path=f"/{db_name}"))
    admin = _pg_admin(base_url)
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{db_name}"')

    api_port, ui_port = _free_port(), _free_port()
    api_base = f"http://127.0.0.1:{api_port}"
    # The browser session's in-process import of celerp.main leaves MODULE_DIR empty in
    # this environment; the subprocesses need the bundled modules, whose sinks the
    # migration writes through.
    module_dirs = ",".join(str(_REPO_ROOT / d) for d in ("default_modules", "premium_modules"))
    # Its own config file, so the modules it turns on and its restart request are its own,
    # not the file the rest of this run shares.
    env = {**os.environ, "DATABASE_URL": db_url, "API_URL": api_base, "CELERP_API_URL": api_base,
           "MODULE_DIR": module_dirs, "CELERP_DATA_DIR": str(tmp_path / "data"),
           "CELERP_CONFIG": str(tmp_path / "config.toml")}

    def spawn():
        procs = []
        try:
            for target, port in (("celerp.main:app", api_port), ("ui.app:app", ui_port)):
                procs.append(subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1",
                     "--port", str(port), "--log-level", "error"],
                    cwd=_REPO_ROOT, env=env))
                _wait_port(port)
        except Exception:
            for proc in procs:
                proc.kill()
            raise
        return tuple(procs)

    servers = None
    try:
        servers = Supervised(spawn, tmp_path / ".restart_requested")
        wait_until_started(api_base)
        yield f"http://127.0.0.1:{ui_port}"
    finally:
        if servers:
            servers.stop()
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        admin.close()


@pytest.fixture
def first_run_page(playwright, first_run_ui):
    browser = playwright.chromium.launch(headless=True)
    ctx = browser.new_context(base_url=first_run_ui, accept_downloads=True)
    page = ctx.new_page()
    yield page
    browser.close()


# ---------------------------------------------------------------------------
# Journey helpers
# ---------------------------------------------------------------------------

def _unit_proofs():
    """The unit proof module, loaded by path: it holds the fake migration API."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "test_setup_migration_ui.py"
    spec = importlib.util.spec_from_file_location("migration_ui_unit_proofs", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_artifact() -> Path:
    """A Manager book that is not the sample: the sample file itself is recognized by
    content and finishes with the sample completion page."""
    from fixtures.manager_io.support import BASIC
    return BASIC


def _upload(page, prepared_by: str) -> None:
    assert "Your file stays on this Celerp server." in page.content()
    assert page.locator('a:has-text("Back")').count() >= 1
    page.set_input_files('input[name="files"]', str(_source_artifact()))
    page.fill('input[name="prepared_by"]', prepared_by)
    page.click('button[type="submit"]:has-text("Analyze")')
    page.wait_for_url(re.compile(r"/migrate/coverage$"))


def _assert_clean_url(page) -> None:
    url = page.url
    assert "scan_token" not in url and "mapping" not in urlsplit(url).query
    assert "scantoken" not in url


def _through_review(page, company_name: str) -> None:
    _assert_clean_url(page)
    body = page.content()
    assert ("No blocking issues found" in body) or ("Cannot move" in body)
    assert "Balances on the cutover date become opening balances." in body
    page.click('button[type="submit"]:has-text("Continue")')
    page.wait_for_url(re.compile(r"/migrate/(mapping|review)$"))
    _assert_clean_url(page)
    if page.url.endswith("/mapping"):
        page.click('button[type="submit"]:has-text("Continue")')
        page.wait_for_url(re.compile(r"/migrate/review$"))
        _assert_clean_url(page)
    body = page.content()
    assert "Prepared by" in body
    page.fill('input[name="company_name"]', company_name)


def _wait_ready(page, run_id: str) -> None:
    page.wait_for_selector(f'a[href="/migrations/{run_id}/verify"]', timeout=_RUN_TIMEOUT_MS)


def _run_id(page) -> str:
    m = re.search(r"/migrations/([0-9a-f-]{36})$", page.url)
    assert m, page.url
    return m.group(1)


def _choose_posting_accounts(page) -> None:
    """Pick an account for each posting account still unchosen, keeping stock and
    everything else apart as a user would: an inventory role takes the account another
    inventory role already uses, any other role the first account offered that is not
    one of those."""
    from celerp.accounting_roles import INVENTORY_VALUE_ROLES

    stock_roles = {f"role.{r.value}" for r in INVENTORY_VALUE_ROLES}
    pickers = page.locator('.combobox-wrap:has(input[type="hidden"][name^="role."])').all()
    fields = [p.locator('input[type="hidden"]') for p in pickers]
    stock = {f.input_value() for f in fields if f.get_attribute("name") in stock_roles and f.input_value()}

    def choice(name: str, values: list[str]) -> str:
        if name in stock_roles:
            return next((v for v in values if v in stock), values[0])
        return next(v for v in values if v not in stock)

    for picker in pickers:
        field = picker.locator('input[type="hidden"]')
        if not field.input_value():
            options = picker.locator(".combobox-option[data-value]").all()
            pick = choice(field.get_attribute("name"), [o.get_attribute("data-value") for o in options])
            picker.locator(".combobox-input").click()
            picker.locator(f'.combobox-option[data-value="{pick}"]').click()


def _verify_and_finish(page, run_id: str, company_name: str) -> None:
    page.goto(f"/migrations/{run_id}/verify")
    for header in ("Check", "Source", "Celerp", "Difference", "Result"):
        assert page.locator(f'th:has-text("{header}")').count() >= 1, header
    for header in ("Source", "Celerp", "Difference"):
        align = page.locator(f'th.cell--number:has-text("{header}")').first.evaluate(
            "el => getComputedStyle(el).textAlign")
        assert align in ("right", "end"), (header, align)
    page.click('a:has-text("Discard migration")')
    page.wait_for_url(re.compile(rf"/migrations/{run_id}/discard$"))
    assert f"Discard the migration into {company_name}?" in page.content()
    page.go_back()
    page.wait_for_url(re.compile(rf"/migrations/{run_id}/verify$"))
    _choose_posting_accounts(page)
    page.click('button:has-text("Finish migration")')
    page.wait_for_url(re.compile(rf"/migrations/{run_id}/complete$"))
    assert "Your company is ready." in page.content()
    assert page.locator('a:has-text("Open company")').count() == 1
    assert page.locator('a:has-text("Move another company")').count() == 1


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------

def test_migration_wizard_browser_first_run(first_run_page):
    page = first_run_page
    page.goto("/setup")
    for label in ("Open a company copy", "Move your books from another system"):
        assert page.locator(f"text={label}").count() >= 1, label
    page.click('a:has-text("Move your books from another system")')
    page.wait_for_url(re.compile(r"/setup/migrate$"))
    assert "Don't see your system? Tell us what you use" in page.content()
    _upload(page, "Example Bookkeeping")
    _through_review(page, "Harbor Goods Ltd")
    page.fill('input[name="name"]', "First Owner")
    page.fill('input[name="email"]', "owner@example.com")
    page.fill('input[name="password"]', "correct-horse-9")
    page.fill('input[name="confirm_password"]', "correct-horse-9")
    page.click('button:has-text("Create company and migrate")')
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    for label in ("Company and settings", "Attachments", "Ready to finish"):
        assert page.locator(f"text={label}").count() >= 1, label

    # Leaving the progress page does not cancel the run.
    page.go_back()
    page.goto(f"/migrations/{run_id}")
    assert "Cancelled" not in page.content()
    _wait_ready(page, run_id)
    _verify_and_finish(page, run_id, "Harbor Goods Ltd")


def test_migration_wizard_browser_existing_owner(page, fresh_company):
    before = fresh_company.get("/companies/me").json()
    page.goto("/setup/new-company")
    page.click('a:has-text("Move from another system")')
    page.wait_for_url(re.compile(r"/setup/new-company/migrate$"))
    _upload(page, "Example Bookkeeping")
    _through_review(page, "Moved Goods Ltd")
    page.click('button:has-text("Create company and migrate")')
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    _wait_ready(page, run_id)
    _verify_and_finish(page, run_id, "Moved Goods Ltd")

    after = fresh_company.get("/companies/me").json()
    assert after == before


def _forget_saved_answers(api) -> None:
    """Forget the posting accounts earlier tests' finished migrations saved for this person,
    so the finish step starts with none preselected."""
    from celerp.importers.adapters.manager_io import ManagerIOAdapter
    assert api.delete(f"/migrations/templates/{ManagerIOAdapter().key}").status_code == 204


def _new_company_run(page, company_name: str) -> str:
    """Move the example file into a new company; returns the run id once it is ready to verify."""
    page.goto("/setup/new-company")
    page.click('a:has-text("Move from another system")')
    page.wait_for_url(re.compile(r"/setup/new-company/migrate$"))
    _upload(page, "Example Bookkeeping")
    _through_review(page, company_name)
    page.click('button:has-text("Create company and migrate")')
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    _wait_ready(page, run_id)
    return run_id


def test_finishing_with_posting_accounts_unchosen_is_refused_in_german(page, ui_server, fresh_company):
    """The refusal names each posting account still unchosen by its German label."""
    from ui import i18n

    _forget_saved_answers(fresh_company)
    run_id = _new_company_run(page, "Unchosen Goods Ltd")
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "domain": host, "path": "/"}])
    try:
        page.goto(f"/migrations/{run_id}/verify")
        unchosen = [s.get_attribute("name").removeprefix("role.")
                    for s in page.locator('input[type="hidden"][name^="role."]').all()
                    if not s.input_value()]
        assert unchosen, "every posting account was preselected"
        page.locator(f"form[action='/migrations/{run_id}/finalize'] button[type='submit']").click()
        page.wait_for_url(re.compile(rf"/migrations/{run_id}/finalize$"))
        body = page.locator("body").inner_text()
    finally:
        page.context.clear_cookies(name="celerp_lang")
    assert "Wählen Sie das Buchungskonto für:" in body, body[:1500]
    assert "Choose the posting account" not in body
    for role in unchosen:
        assert i18n.t(f"posting.role.{role}", lang="de") in body, (role, body[:1500])


_PAST_THE_CARD = """() => {
  const card = document.querySelector('.auth-card');
  if (!card || !card.querySelector('table')) return null;
  const edge = card.getBoundingClientRect().right - parseFloat(getComputedStyle(card).paddingRight);
  const tables = [...card.querySelectorAll('table')].map(t => t.closest('.table-scroll-wrap') || t);
  return [document.documentElement.scrollWidth - document.documentElement.clientWidth,
          ...tables.map(b => Math.round(b.getBoundingClientRect().right - edge))];
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_every_wizard_page_fits_the_screen(page, ui_server, fresh_company, width):
    """No wizard page scrolls sideways and every table stays inside the card: a coverage
    note or a check label carrying an ID wraps, and on a phone a table still wider than the
    card scrolls inside its own box. The steps before the run are read as the helper walks
    them; the run's own pages are read again in German, the longer labels."""
    over = {}

    def measure(_page=None):
        found = page.evaluate(_PAST_THE_CARD)
        if found is not None:
            over[re.sub(r"[0-9a-f-]{36}", "{run}", urlsplit(page.url).path) + f" ({lang})"] = found

    lang = "en"
    page.set_viewport_size({"width": width, "height": 900})
    page.on("load", measure)
    run_id = _new_company_run(page, "Fitting Goods Ltd")
    page.remove_listener("load", measure)
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "domain": host, "path": "/"}])
    lang = "de"
    try:
        for path in (f"/migrations/{run_id}", f"/migrations/{run_id}/verify"):
            page.goto(path)
            measure()
    finally:
        page.context.clear_cookies(name="celerp_lang")
    assert {"/setup/new-company/migrate/coverage (en)", "/setup/new-company/migrate/review (en)",
            "/migrations/{run} (de)", "/migrations/{run}/verify (de)"} <= set(over), sorted(over)
    wide = {where: px for where, px in over.items() if max(px) > 0}
    assert not wide, f"past the screen or the card at {width}px (page, then each table): {wide}"


def test_an_added_posting_account_the_chart_refuses_is_refused_in_german(page, ui_server, fresh_company, monkeypatch):
    """Adding the proposed accounts when the chart cannot take them (here a code longer
    than the chart allows) shows the chart's refusal in German."""
    from celerp.services import posting_readiness

    real = posting_readiness._proposals
    monkeypatch.setattr(posting_readiness, "_proposals", lambda chart, reserved: {
        role: {**proposal, "code": "P" * 30 + proposal["code"]}
        for role, proposal in real(chart, reserved).items()})
    _forget_saved_answers(fresh_company)
    page.goto("/setup/new-company")
    page.click('a:has-text("Move from another system")')
    page.wait_for_url(re.compile(r"/setup/new-company/migrate$"))
    _upload(page, "Example Bookkeeping")
    _through_review(page, "Refused Account Goods Ltd")
    page.click('button:has-text("Create company and migrate")')
    page.wait_for_url(re.compile(r"/migrations/[0-9a-f-]{36}$"))
    run_id = _run_id(page)
    _wait_ready(page, run_id)
    host = ui_server.split("//", 1)[1].split(":", 1)[0]
    page.context.add_cookies([{"name": "celerp_lang", "value": "de", "domain": host, "path": "/"}])
    try:
        page.goto(f"/migrations/{run_id}/verify")
        added = page.evaluate("""() => [...document.querySelectorAll(
                'input[type="hidden"][name^="role."]')]
            .filter(f => !f.value).map(f => { f.value = "__new__"; return f.name; })""")
        assert added, "every posting account was preselected"
        page.locator(f"form[action='/migrations/{run_id}/finalize'] button[type='submit']").click()
        page.wait_for_url(re.compile(rf"/migrations/{run_id}/finalize$"))
        body = page.locator("body").inner_text()
    finally:
        page.context.clear_cookies(name="celerp_lang")
    assert "Kontonummer darf höchstens 32 Zeichen lang sein." in body, body[:1500]
    assert "must be 32 characters" not in body


@pytest.fixture
def fake_migration_api(ui_server, monkeypatch):
    """Route only the UI's migration API calls to the in-test fake."""
    import ui.api_client as api
    from ui.config import API_BASE

    fake = _unit_proofs().FakeMigrationAPI()
    # Every other call goes to the real API through the UI's own pool, which the UI
    # server closes when it stops.
    real = api._get_transport()

    class _Split(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.url.path.startswith("/migrations"):
                return await fake.handle(request)
            return await real.handle_async_request(request)

    def _factory(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return httpx.AsyncClient(base_url=API_BASE, headers=merged, transport=_Split(),
                                 follow_redirects=follow_redirects, timeout=timeout)

    monkeypatch.setattr(api, "_local_client", _factory)
    return fake


def test_migration_ui_mapping_uses_shared_searchable_select(page, fake_migration_api):
    first_option = _unit_proofs()._ACCOUNT_OPTIONS[0]

    def _to_coverage():
        page.goto("/setup/new-company/migrate")
        page.set_input_files('input[name="files"]', str(Path(__file__)))
        page.click('button[type="submit"]:has-text("Analyze")')
        page.wait_for_url(re.compile(r"/migrate/coverage$"))
        page.click('button[type="submit"]:has-text("Continue")')

    _to_coverage()
    page.wait_for_url(re.compile(r"/migrate/mapping$"))
    wrap = page.locator(".combobox-wrap")
    assert wrap.count() == 1
    hidden = wrap.locator('input[type="hidden"]')
    assert hidden.input_value() == first_option
    box = wrap.locator(".combobox-input")
    box.click()
    page.wait_for_selector(".combobox-list.open", timeout=3000)
    box.fill("Income account 11")
    assert page.locator(".combobox-option:visible").filter(has_text="Income account 11").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector(".combobox-list.open", state="detached", timeout=3000)
    assert hidden.input_value() == first_option

    fake_migration_api.with_questions = False
    _to_coverage()
    page.wait_for_url(re.compile(r"/migrate/review$"))
    assert page.locator(".combobox-wrap").count() == 0


def test_escape_leaves_wizard_fields(page):
    """Escape exits a text field on the wizard pages, which carry the shared client script."""
    page.goto("/setup/new-company/migrate")
    field = page.locator("#prepared_by")
    field.click()
    expect(field).to_be_focused()
    page.keyboard.press("Escape")
    expect(field).not_to_be_focused()


def test_an_error_box_keeps_a_gap_above_what_follows(page):
    """A refusal box does not sit flush against the field or button after it."""
    page.goto("/setup/new-company/migrate")
    page.click('button[type="submit"]:has-text("Analyze")')  # nothing chosen
    box = page.locator(".flash--error")
    box.wait_for()
    gap = box.evaluate("b => b.nextElementSibling.getBoundingClientRect().top - b.getBoundingClientRect().bottom")
    assert gap >= 8
