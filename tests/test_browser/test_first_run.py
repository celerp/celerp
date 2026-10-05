# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""First run on a fresh install: one setup form, then the dashboard.

The suite's shared servers already carry a seeded owner, so these tests boot their
own API and UI processes against their own empty database, and empty it again
before each test: every test starts exactly where a new user starts.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser

_REPO = Path(__file__).resolve().parents[2]
_worker = os.environ.get("PYTEST_XDIST_WORKER", "")
_offset = int(_worker[2:]) if _worker[2:].isdigit() else 0
_BASE = int(os.environ.get("BROWSER_TEST_PORT_BASE", "18000"))
_API_PORT = _BASE + 200 + _offset
_UI_PORT = _BASE + 300 + _offset
_PASSWORD = "FirstRun123!"


def _pg(dbname: str):
    import psycopg2
    parts = urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn = psycopg2.connect(host=parts.hostname, port=parts.port, user=parts.username,
                            password=parts.password, dbname=dbname)
    conn.autocommit = True
    return conn


def _wait_http(url: str, proc: subprocess.Popen, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server for {url} exited with {proc.returncode}")
        try:
            httpx.get(url, timeout=1.0)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise RuntimeError(f"{url} did not answer within {timeout}s")


class _FreshInstall:
    """An API and a UI process on their own database, with no owner yet."""

    def __init__(self) -> None:
        url = os.environ["DATABASE_URL"]
        parts = urlsplit(url)
        self.db = parts.path.lstrip("/") + "_first_run"
        self.db_url = url.rsplit("/", 1)[0] + "/" + self.db
        self.api = f"http://127.0.0.1:{_API_PORT}"
        self.ui = f"http://127.0.0.1:{_UI_PORT}"
        self.tmp = Path(tempfile.mkdtemp(prefix="celerp-first-run-"))
        self.procs: list[subprocess.Popen] = []

    def _spawn(self, app: str, port: int, env: dict) -> subprocess.Popen:
        log = open(self.tmp / f"{port}.log", "ab")
        return subprocess.Popen(
            [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port)],
            cwd=_REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
        )

    def start(self) -> None:
        for port in (_API_PORT, _UI_PORT):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"port {port} is in use; set BROWSER_TEST_PORT_BASE")
        cfg = self.tmp / "config"
        shutil.rmtree(cfg, ignore_errors=True)
        cfg.mkdir()
        env = dict(os.environ)
        env.pop("PYTEST_XDIST_WORKER", None)
        env.update({
            "DATABASE_URL": self.db_url,
            "CELERP_CONFIG": str(cfg / "config.toml"),
            "MODULE_DIR": ",".join(str(_REPO / d) for d in ("default_modules", "premium_modules")
                                   if (_REPO / d).is_dir()),
            "API_URL": self.api,
            "CELERP_API_URL": self.api,
        })
        api = self._spawn("celerp.main:app", _API_PORT, env)
        self.procs.append(api)
        _wait_http(f"{self.api}/health", api)
        ui = self._spawn("ui.app:app", _UI_PORT, env)
        self.procs.append(ui)
        _wait_http(f"{self.ui}/health", ui)

    def stop(self) -> None:
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        self.procs = []

    def reset(self) -> None:
        """Back to a new install: no owner, no company, a fresh config."""
        self.stop()
        conn = _pg(self.db)
        try:
            with conn.cursor() as cur:
                cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        finally:
            conn.close()
        self.start()

    def company(self, ctx) -> dict:
        """The signed-in browser's company, read with its own session token."""
        token = next(c["value"] for c in ctx.cookies() if c["name"] == "celerp_token")
        r = httpx.get(f"{self.api}/companies/me", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return r.json()


@pytest.fixture(scope="module")
def install():
    fresh = _FreshInstall()
    conn = _pg("postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{fresh.db}" WITH (FORCE)')
            cur.execute(f'CREATE DATABASE "{fresh.db}"')
    finally:
        conn.close()
    fresh.start()
    yield fresh
    fresh.stop()
    conn = _pg("postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{fresh.db}" WITH (FORCE)')
    finally:
        conn.close()
    shutil.rmtree(fresh.tmp, ignore_errors=True)


@pytest.fixture
def fresh(install):
    """The install with no owner: emptied again when an earlier test finished setup."""
    if httpx.get(f"{install.api}/auth/bootstrap-status", timeout=10).json().get("bootstrapped"):
        install.reset()
    return install


def _context(playwright, *, width: int = 1280, locale: str = "en-US", timezone_id: str = "Asia/Bangkok",
             **kw):
    browser = playwright.chromium.launch()
    ctx = browser.new_context(viewport={"width": width, "height": 900}, locale=locale,
                              timezone_id=timezone_id, **kw)
    return browser, ctx


def _no_sideways_scroll(page: Page, where: str) -> None:
    overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"{where}: page scrolls sideways by {overflow}px"


def _choose(page: Page, name: str, value: str) -> None:
    wrap = page.locator(f'.combobox-wrap:has(input[type="hidden"][name="{name}"])')
    wrap.locator(".combobox-input").click()
    wrap.locator(f'.combobox-option[data-value="{value}"]').click()
    expect(wrap.locator(f'input[type="hidden"][name="{name}"]')).to_have_value(value)


def _fill_text_fields(page: Page, email: str) -> None:
    page.fill("#company_name", "First Run Trading")
    page.fill("#name", "Owner Person")
    page.fill("#email", email)
    page.fill("#password", _PASSWORD)
    page.fill("#confirm_password", _PASSWORD)


def _hidden(page: Page, name: str) -> str:
    return page.locator(f'#setup-form input[type="hidden"][name="{name}"]').input_value()


@pytest.mark.parametrize("width", [390, 1280])
def test_first_run_one_form_to_dashboard(playwright, fresh, width):
    """INV-1, INV-12: one form, one submit, then the dashboard; nothing scrolls sideways."""
    browser, ctx = _context(playwright, width=width)
    try:
        page = ctx.new_page()
        posts: list[str] = []
        page.on("request", lambda r: posts.append(r.url)
                if r.method == "POST" and r.is_navigation_request() else None)
        page.goto(f"{fresh.ui}/", wait_until="networkidle")
        assert urlsplit(page.url).path == "/setup", page.url
        assert page.locator("form").count() == 1, "the first screen must hold exactly one form"
        _no_sideways_scroll(page, "/setup")
        _fill_text_fields(page, f"owner{width}@first-run.test")
        _choose(page, "vertical", "blank")
        _choose(page, "currency", "THB")
        page.locator('#setup-form button[type="submit"]').scroll_into_view_if_needed()
        expect(page.locator('#setup-form button[type="submit"]')).to_be_visible()
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        if urlsplit(page.url).path == "/setup/activating":
            _no_sideways_scroll(page, "/setup/activating")
            page.wait_for_url(f"{fresh.ui}/dashboard", timeout=120000)
        page.wait_for_load_state("load")
        _no_sideways_scroll(page, "/dashboard")
        assert posts == [f"{fresh.ui}/setup"], f"expected exactly one form submit, got {posts}"
        assert page.locator("form#setup-form").count() == 0
    finally:
        browser.close()


@pytest.mark.parametrize("locale,timezone_id,expected", [
    ("en-US", "Asia/Bangkok", "THB"),
    ("de-DE", "Europe/Berlin", "EUR"),
    ("en", "UTC", ""),
    # Chrome reports this zone by its old name, Asia/Calcutta.
    ("en-US", "Asia/Kolkata", "INR"),
])
def test_currency_guess_uses_timezone_country_first(playwright, fresh, locale, timezone_id, expected):
    """INV-14: the timezone's country decides before the language region; unknown is no guess."""
    browser, ctx = _context(playwright, locale=locale, timezone_id=timezone_id)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        assert _hidden(page, "currency") == expected
        shown = page.locator('.combobox-wrap:has(input[name="currency"]) .combobox-input').input_value()
        assert (expected in shown) if expected else shown == "", shown
    finally:
        browser.close()


def test_timezone_hidden_field_filled_from_browser(playwright, fresh):
    """A1: the browser's timezone is stored with the company, by its current name even
    though Chrome reports this one by its old name, America/Buenos_Aires."""
    browser, ctx = _context(playwright, timezone_id="America/Argentina/Buenos_Aires")
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        assert _hidden(page, "timezone") == "America/Argentina/Buenos_Aires"
        _fill_text_fields(page, "tz@first-run.test")
        _choose(page, "vertical", "blank")
        # The peso is not in the app's currency list, so nothing is suggested.
        assert _hidden(page, "currency") == ""
        _choose(page, "currency", "USD")
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        company = fresh.company(ctx)
        settings = company.get("settings") or {}
        assert settings.get("timezone") == "America/Argentina/Buenos_Aires"
        assert settings.get("currency") == "USD"
    finally:
        browser.close()


def test_typed_values_survive_leaving_for_an_option(playwright, fresh):
    """INV-3: leaving for Restore and coming back keeps everything but the passwords;
    a successful submit clears what was kept."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        _fill_text_fields(page, "kept@first-run.test")
        _choose(page, "vertical", "blank")
        _choose(page, "currency", "EUR")
        page.locator(".start-option a").first.click()
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url).path != "/setup"
        page.locator('a[href="/setup"]').first.click()
        page.wait_for_url(f"{fresh.ui}/setup")
        page.wait_for_load_state("networkidle")
        assert page.input_value("#company_name") == "First Run Trading"
        assert page.input_value("#name") == "Owner Person"
        assert page.input_value("#email") == "kept@first-run.test"
        assert _hidden(page, "vertical") == "blank"
        assert _hidden(page, "currency") == "EUR"
        assert page.input_value("#password") == ""
        assert page.input_value("#confirm_password") == ""
        page.fill("#password", _PASSWORD)
        page.fill("#confirm_password", _PASSWORD)
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        kept = page.evaluate("() => sessionStorage.getItem('celerp-setup-form')")
        assert kept is None, kept
    finally:
        browser.close()


def test_create_button_disables_on_submit(playwright, fresh):
    """INV-4: Create cannot be pressed twice."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        # Runs after the form's own submit handler; the record outlives the navigation.
        page.evaluate("""() => document.getElementById('setup-form').addEventListener('submit', () =>
            sessionStorage.setItem('probe-disabled',
                String(document.querySelector('#setup-form button[type=submit]').disabled)))""")
        _fill_text_fields(page, "twice@first-run.test")
        _choose(page, "vertical", "blank")
        _choose(page, "currency", "EUR")
        page.locator('#setup-form button[type="submit"]').click()
        page.wait_for_url(lambda u: urlsplit(u).path in ("/dashboard", "/setup/activating"), timeout=30000)
        assert page.evaluate("() => sessionStorage.getItem('probe-disabled')") == "true"
    finally:
        browser.close()


def test_activating_page_shows_failure_and_retry_after_bounded_wait(page, ui_server):
    """INV-1: modules that never come up end in an honest failure with a retry, in real
    time and within the bound, never an endless spinner."""
    page.route("**/setup/activating-status*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"phase": "loading", "requested": 1, "loaded": 0,'
             ' "modules": [{"name": "celerp-docs", "label": "Documents", "running": false}]}'))
    page.goto(f"{ui_server}/setup/activating", wait_until="domcontentloaded")
    failed = page.locator("#activating-failed")
    expect(failed).to_be_visible(timeout=45000)
    expect(failed.locator('a[href="/setup/activating"]')).to_be_visible()
    expect(page.locator(".activating-spinner")).to_be_hidden()
    assert urlsplit(page.url).path == "/setup/activating"


def test_setup_copy_fits_one_line(playwright, fresh):
    """INV-13: at desktop width each option description and each hint is one line."""
    browser, ctx = _context(playwright, width=1280)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        lines = page.evaluate("""() => Array.from(document.querySelectorAll(
              '.setup-card .start-option-desc, .setup-card .start-option-note,'
            + ' .setup-card .setup-options-note, .setup-card .form-hint'))
          .map(el => {
            const lh = parseFloat(getComputedStyle(el).lineHeight);
            return {text: el.textContent, lines: Math.round(el.getBoundingClientRect().height / lh)};
          })""")
        assert len(lines) >= 6, lines
        wrapped = [l for l in lines if l["lines"] != 1]
        assert not wrapped, wrapped
    finally:
        browser.close()


def test_keyboard_and_focus(playwright, fresh):
    """INV-12: first field focused, Esc closes both dropdowns, Enter submits, and an
    error focuses the field it is about."""
    browser, ctx = _context(playwright, timezone_id="UTC", locale="en")
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        assert page.evaluate("() => document.activeElement.id") == "company_name"
        for name in ("vertical", "currency"):
            wrap = page.locator(f'.combobox-wrap:has(input[type="hidden"][name="{name}"])')
            wrap.locator(".combobox-input").click()
            expect(wrap.locator(".combobox-list.open")).to_be_visible()
            page.keyboard.press("Escape")
            expect(wrap.locator(".combobox-list.open")).to_have_count(0)
        _fill_text_fields(page, "keys@first-run.test")
        _choose(page, "vertical", "blank")
        # No currency: the server refuses and says which field.
        page.focus("#confirm_password")
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        assert urlsplit(page.url).path == "/setup"
        expect(page.locator(".flash")).to_be_visible()
        active = page.evaluate("() => document.activeElement.closest('.combobox-wrap')"
                               " && document.activeElement.closest('.combobox-wrap')"
                               ".querySelector('input[type=hidden]').name")
        assert active == "currency", active
    finally:
        browser.close()


def test_inputs_carry_autocomplete_hints(playwright, fresh):
    """INV-12: browsers can fill the form in."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        hints = {f: page.get_attribute(f"#{f}", "autocomplete")
                 for f in ("company_name", "name", "email", "password", "confirm_password")}
        assert hints == {"company_name": "organization", "name": "name", "email": "email",
                         "password": "new-password", "confirm_password": "new-password"}, hints
    finally:
        browser.close()


@pytest.mark.parametrize("width,height", [(390, 700), (1280, 800)])
def test_dropdowns_stay_on_screen(playwright, fresh, width, height):
    """INV-12: an open business type or currency list sits fully on screen, so the last
    option ("Other / general") can be picked without scrolling the page."""
    browser = playwright.chromium.launch()
    ctx = browser.new_context(viewport={"width": width, "height": height})
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        for name in ("vertical", "currency"):
            wrap = page.locator(f'.combobox-wrap:has(input[type="hidden"][name="{name}"])')
            wrap.locator(".combobox-input").scroll_into_view_if_needed()
            wrap.locator(".combobox-input").click()
            box = wrap.locator(".combobox-list.open").bounding_box()
            assert box and box["y"] >= 0 and box["y"] + box["height"] <= height, (name, box)
            page.keyboard.press("Escape")
        _choose(page, "vertical", "blank")
    finally:
        browser.close()


def test_dropdown_reopens_on_click_after_esc(playwright, fresh):
    """INV-12: Esc closes a searchable dropdown and a click on the field opens it again."""
    browser, ctx = _context(playwright)
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        wrap = page.locator('.combobox-wrap:has(input[type="hidden"][name="vertical"])')
        wrap.locator(".combobox-input").click()
        expect(wrap.locator(".combobox-list.open")).to_be_visible()
        page.keyboard.press("Escape")
        expect(wrap.locator(".combobox-list.open")).to_have_count(0)
        wrap.locator(".combobox-input").click()
        expect(wrap.locator(".combobox-list.open")).to_be_visible()
    finally:
        browser.close()


_LOCALE_DIR = Path(__file__).resolve().parents[2] / "ui" / "locales"
_SETUP_SCREEN_KEYS = (
    "page.set_up_your_workspace", "btn.create_workspace", "setup.choose_business_type",
    "setup.options_heading", "setup.option_restore_title", "setup.option_restore_desc",
    "setup.option_move_title", "setup.option_move_desc", "setup.more_coming_soon",
    "setup.not_sure_yet", "setup.later_from_dashboard", "setup.currency_hint",
    "setup.business_type_hint", "setup.password_hint",
)


@pytest.mark.parametrize("width", [390, 1280])
def test_setup_screen_in_german(playwright, fresh, width):
    """INV-20 German gate: a de-DE browser gets the whole setup screen in German, with
    no English copy left, nothing scrolling sideways, and the explainers still one line
    at desktop width."""
    import json

    en = json.loads((_LOCALE_DIR / "en.json").read_text())
    de = json.loads((_LOCALE_DIR / "de.json").read_text())
    browser, ctx = _context(playwright, width=width, locale="de-DE", timezone_id="Europe/Berlin")
    try:
        page = ctx.new_page()
        page.goto(f"{fresh.ui}/setup", wait_until="networkidle")
        text = page.locator("body").inner_text()
        for key in _SETUP_SCREEN_KEYS:
            de_value = de[key].split("{")[0].strip()
            en_value = en[key].split("{")[0].strip()
            assert de_value != en_value, f"{key} is not translated"
            assert de_value in text, f"{key}: {de_value!r} missing"
            assert en_value not in text, f"{key}: English {en_value!r} on the German page"
        _no_sideways_scroll(page, f"setup de {width}")
        if width == 1280:
            wrapped = page.evaluate("""() => Array.from(document.querySelectorAll(
                  '.setup-card .start-option-desc, .setup-card .setup-options-note, .setup-card .form-hint'))
              .filter(el => Math.round(el.getBoundingClientRect().height
                                       / parseFloat(getComputedStyle(el).lineHeight)) !== 1)
              .map(el => el.textContent)""")
            assert not wrapped, wrapped
    finally:
        browser.close()
