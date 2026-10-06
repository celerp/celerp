# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The screens shown before signing in read in the visitor's language: first-run setup,
backup restore, start a company, sign in and its error, forgot and reset password, and
the page shown when the local service cannot be reached. Each is rendered in every UI
language and compared with its English rendering; a text, placeholder or title still
reading as in English fails, unless the catalog has it reviewed as the same in that
language (tests/i18n_source_identical_allowlist.json)."""
from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from celerp.main import app
from ui import i18n
from ui.app import app as ui_app
from ui.components.currency import currency_label

_ALLOWLIST = json.loads((Path(__file__).parent / "i18n_source_identical_allowlist.json").read_text())
_LANGS = sorted(i18n._DISK_LANGS - {"en"})
_ATTRS = {"placeholder", "title", "aria-label", "alt"}
# Read the same in every language: the product name, and the dots standing in for a password.
_NOT_WORDS = {"Celerp", "••••••••"}


class _Shown(HTMLParser):
    """Every text node and labelling attribute a visitor reads; scripts, styles and
    aria-hidden decoration (an icon beside a title) are not read."""

    def __init__(self):
        super().__init__()
        self.shown: set[str] = set()
        self._hidden: list[list] = []  # [tag, open count] of each hiding element

    def handle_starttag(self, tag, attrs):
        if self._hidden and self._hidden[-1][0] == tag:
            self._hidden[-1][1] += 1
        elif tag in ("script", "style") or ("aria-hidden", "true") in attrs:
            self._hidden.append([tag, 1])
        if not self._hidden:
            self.shown.update(v.strip() for k, v in attrs if k in _ATTRS and v and v.strip())

    def handle_endtag(self, tag):
        if self._hidden and self._hidden[-1][0] == tag:
            self._hidden[-1][1] -= 1
            if not self._hidden[-1][1]:
                self._hidden.pop()

    def handle_data(self, data):
        if not self._hidden and data.strip():
            self.shown.add(data.strip())


def _shown(html: str) -> set[str]:
    parser = _Shown()
    parser.feed(html)
    return parser.shown - _NOT_WORDS


def _same_in(lang: str) -> set[str]:
    """English texts reviewed as the same in *lang*. A currency shows as its code and
    name, so a name that is the same leaves the whole label the same."""
    en = i18n._cached_load("en")
    keys = [k for k in _ALLOWLIST.get(lang, ()) if k in en]
    return {en[k] for k in keys} | {currency_label(k.removeprefix("currency.name."))
                                    for k in keys if k.startswith("currency.name.")}


def _in_process(tok=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", follow_redirects=follow_redirects,
                       headers={**(headers or {}), **({"Authorization": f"Bearer {tok}"} if tok else {})})


def _unreachable(*_a, **_k):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    return AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://test")


async def _render(ui, lang: str, before_setup: bool) -> dict[str, str]:
    h = {"Accept-Language": lang}
    if before_setup:
        pages = {url: (await ui.get(url, headers=h)) for url in ("/setup", "/setup/import-backup")}
    else:
        pages = {url: (await ui.get(url, headers=h)) for url in
                 ("/login", "/forgot-password", "/reset-password?token=x", "/setup/start-company")}
        pages["sign-in error"] = await ui.post("/login", headers=h, data={"email": "pre@test.example",
                                                                         "password": "not-the-password"})
    for name, r in pages.items():
        assert r.status_code == 200, (lang, name, r.status_code, r.headers.get("location"))
    return {name: r.text for name, r in pages.items()}


async def _english_left(ui, before_setup: bool) -> dict[tuple[str, str], list[str]]:
    english = await _render(ui, "en", before_setup)
    left = {}
    for lang in _LANGS:
        for name, html in (await _render(ui, lang, before_setup)).items():
            same = _shown(html) & _shown(english[name]) - _same_in(lang)
            if same:
                left[(lang, name)] = sorted(same)
    return left


@pytest.fixture
def _email_install():
    from ui.routes import auth
    with patch.object(auth._settings, "smtp_host", "smtp.test.example"):
        yield


@pytest.mark.asyncio
async def test_first_run_screens_read_in_every_language(client):
    with patch("ui.api_client._local_client", _in_process):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            assert await _english_left(ui, before_setup=True) == {}


@pytest.mark.asyncio
async def test_sign_in_screens_read_in_every_language(client, _email_install):
    r = await client.post("/auth/register", json={"company_name": "Pre Co", "email": "pre@test.example",
                                                  "name": "Pre", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    with patch("ui.api_client._local_client", _in_process):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            assert await _english_left(ui, before_setup=False) == {}


@pytest.mark.asyncio
async def test_the_service_unavailable_page_reads_in_every_language():
    with patch("ui.api_client._local_client", _unreachable):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            english = (await ui.get("/login")).text
            left = {lang: sorted(_shown((await ui.get("/login", headers={"Accept-Language": lang})).text)
                                 & _shown(english) - _same_in(lang)) for lang in _LANGS}
    assert {lang: s for lang, s in left.items() if s} == {}
