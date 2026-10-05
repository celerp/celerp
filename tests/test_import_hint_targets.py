# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The dashboard card promises "Import is always at the top of the page", and
its links open the list pages with an arrow on that page's Import button. Every
list page with an Import button therefore carries exactly one data-import-hint, in
the page header's action bar. The UI runs against the real API in process.
"""
from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

_EN = json.loads((Path(__file__).parent.parent / "ui" / "locales" / "en.json").read_text())

_LIST_PAGES = ["/inventory", "/contacts/customers", "/contacts/vendors", "/docs", "/lists",
               "/subscriptions?direction=sales", "/subscriptions?direction=purchasing"]


@pytest.fixture()
async def owner_ui(client):
    from celerp.main import app as api_app
    from ui.app import app as ui_app

    r = await client.post("/auth/register", json={
        "company_name": "Hint Co", "email": f"hint-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]

    def _bridged(tok, timeout=10.0):
        return AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test",
                           headers={"Authorization": f"Bearer {tok}"}, follow_redirects=True)

    with patch("ui.api_client._client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                               follow_redirects=False, cookies={"celerp_token": token}) as ui:
            yield ui


def _hint_count(html: str) -> int:
    """data-import-hint attributes in the markup (the shell's script names the selector)."""
    return re.sub(r"<script\b.*?</script>", "", html, flags=re.S).count("data-import-hint")


def _header_actions(html: str) -> str:
    """The page header's action bar, nested divs (the search bar) included."""
    start = html.find('<div class="page-actions">')
    if start < 0:
        return ""
    depth = 0
    for tag in re.finditer(r"<(/?)div\b[^>]*>", html[start:]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return html[start:start + tag.end()]
    return ""


@pytest.mark.parametrize("path", _LIST_PAGES)
async def test_every_list_page_has_exactly_one_import_hint(owner_ui, path):
    r = await owner_ui.get(path)
    assert r.status_code == 200, (path, r.status_code)
    assert _hint_count(r.text) == 1, path
    actions = _header_actions(r.text)
    hinted = re.search(r'<a[^>]*data-import-hint[^>]*>', actions)
    assert hinted, f"{path}: the hinted Import button sits in the header action bar"
    assert "/import" in hinted.group(0)
    # The arrow says "Click Import", so the button it points at says Import.
    label = re.search(r'<a[^>]*data-import-hint[^>]*>([^<]*)</a>', actions).group(1).strip()
    assert label == _EN["btn.import"], (path, label)


@pytest.mark.parametrize("path", ["/subscriptions?direction=sales", "/subscriptions?direction=purchasing"])
async def test_subscriptions_shell_follows_browser_language(owner_ui, path):
    """The shell around a subscriptions list is built for this request: German
    browser, German arrow text and nav, and the signed-in user's menu."""
    de = json.loads((Path(__file__).parent.parent / "ui" / "locales" / "de.json").read_text())
    r = await owner_ui.get(path, headers={"Accept-Language": "de-DE,de;q=0.9"})
    assert r.status_code == 200
    assert json.dumps(de["shell.import_hint"], ensure_ascii=False)[1:-1] in r.text
    assert _EN["shell.import_hint"] not in r.text


async def test_inventory_empty_state_import_link_is_not_a_second_target(owner_ui):
    # A search with no match shows the empty state; the arrow still has one target.
    r = await owner_ui.get("/inventory?q=no-such-item-anywhere")
    assert r.status_code == 200
    assert _hint_count(r.text) == 1


@pytest.mark.parametrize("path, home", [
    ("/crm/import/contacts", "/contacts/customers"),
    ("/lists/import", "/lists"),
    ("/subscriptions/import", "/subscriptions"),
])
async def test_contacts_import_back_button_label(owner_ui, path, home):
    """An import page's back button goes to its own list and says Back, never
    "Back to settings" (the contacts page was fixed with the setup form; lists and
    subscriptions had the same mislabel)."""
    import html as _html
    r = await owner_ui.get(path)
    assert r.status_code == 200
    back = re.search(rf'<a[^>]*href="{re.escape(home)}"[^>]*>([^<]*)</a>', _header_actions(r.text))
    assert back, f"{path}: a back button to {home}"
    assert _html.unescape(back.group(1)).strip() == _EN["btn.back"]
    assert "Back to settings" not in r.text
