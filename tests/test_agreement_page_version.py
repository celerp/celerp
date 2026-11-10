# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every save a draft agreement's page sends hands the page the version it stored.

The page holds the record's version (window._celerpEntityVersion) and sends it with each lines
save; a save that moves the version and answers without it leaves the page one version behind, and
its next lines save is refused as someone else's change. Generated from the rendered page: every
control that posts, patches, puts or deletes, and every action posted after the lines save
(persist_then_post), is called on a fresh draft, and when the stored version moved the answer
either reloads the page (HX-Redirect) or carries celerpListVersion with the stored version. A new
control on the page is in the census the moment it renders."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "default_modules" / "celerp-subscriptions" / "tests"))
from agreement_kit import agreement, contact, register, schedule  # noqa: E402

_METHODS = ("post", "patch", "put", "delete")
_ACTION = re.compile(r"htmx\.ajax\('POST','([^']+)'")


def _ui(monkeypatch, headers: dict) -> httpx.AsyncClient:
    """The UI as the browser reaches it, rendering from the real API."""
    import celerp.main
    import ui.api_client as api
    from ui.app import app as ui_app

    def make(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = {**(headers or {}), **({"Authorization": f"Bearer {token}"} if token else {})}
        return httpx.AsyncClient(base_url="http://api", headers=merged, timeout=timeout,
                                 transport=httpx.ASGITransport(app=celerp.main.app), follow_redirects=follow_redirects)

    monkeypatch.setattr(api, "_local_client", make)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://localhost",
                             cookies={"celerp_token": headers["Authorization"][7:]})


def _value(el) -> str:
    """A value the control does not already hold."""
    if el.name == "select":
        values = [o.get("value", o.text) for o in el.find_all("option")]
        chosen = el.find("option", selected=True)
        current = chosen.get("value", chosen.text) if chosen else values[0]
        return next((v for v in values if v != current), current)
    current = el.get("value") or ""
    if el.get("type") == "date":
        return "2027-05-01" if current != "2027-05-01" else "2027-06-01"
    if el.get("type") == "number":
        return str(int(current or el.get("min") or 0) + 1)
    return "Census text"


def _form(el) -> dict:
    fields = el.find_all(["input", "select", "textarea"]) if el.name == "form" else [el]
    return {f["name"]: _value(f) for f in fields if f.get("name") and f.get("type") != "hidden"}


def _controls(html: str, eid: str) -> dict[tuple[str, str], dict]:
    """(method, path with the agreement as {eid}) -> what the page sends for it."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict = {}
    for m in _METHODS:
        for el in soup.find_all(attrs={f"hx-{m}": True}):
            found[(m, el[f"hx-{m}"].replace(eid, "{eid}"))] = _form(el)
    for el in soup.find_all(attrs={"onclick": _ACTION}):
        found[("post", _ACTION.search(el["onclick"]).group(1).replace(eid, "{eid}"))] = {}
    return {k: v for k, v in found.items() if "{eid}" in k[1]}


# The settings a draft's page starts from: as drafted, and billing and sending on its own (where
# going back to billing by hand turns sending off, and the answer says so).
_SETUPS = {"drafted": {}, "sending": {"generation_mode": "auto_invoice", "auto_send": True}}


async def _draft(client, ui, headers: dict, setup: dict) -> tuple[str, dict]:
    eid = await agreement(client, headers, start="2027-03-01", activate=False,
                          contact_id=await contact(client, headers, "census@test.test"))
    if setup:
        r = await schedule(client, headers, eid, **setup)
        assert r.status_code == 200, r.text
    page = await ui.get(f"/subscriptions/{eid}")
    assert page.status_code == 200, page.status_code
    return eid, _controls(page.text, eid)


async def _version(client, headers: dict, eid: str):
    r = await client.get(f"/docs/{eid}", headers=headers)
    return r.json().get("version") if r.status_code == 200 else None


@pytest.mark.parametrize("setup", sorted(_SETUPS))
async def test_every_save_on_a_draft_agreement_page_hands_the_page_the_stored_version(client, monkeypatch, setup):
    headers = await register(client)
    async with _ui(monkeypatch, headers) as ui:
        _, census = await _draft(client, ui, headers, _SETUPS[setup])
        assert any("/schedule/" in path for _, path in census), census
        moved, wrong = [], []
        for method, path in census:
            eid, controls = await _draft(client, ui, headers, _SETUPS[setup])
            before = await _version(client, headers, eid)
            r = await ui.request(method.upper(), path.replace("{eid}", eid), data=controls[(method, path)],
                                 headers={"HX-Request": "true"})
            stored = await _version(client, headers, eid)
            if "hx-redirect" in r.headers or stored == before:
                continue
            moved.append(path)
            told = json.loads(r.headers.get("hx-trigger") or "{}").get("celerpListVersion") or {}
            if told.get("version") != stored:
                wrong.append((method, path, r.status_code, stored, told))
    assert any("/schedule/" in path for path in moved), moved
    assert not wrong, "\n".join(map(str, wrong))
