# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The browser tab shows the Celerp shield: every page shell links the shield icon
set (ico, 32px png, apple-touch-icon), and each linked file is served."""

from __future__ import annotations

from html.parser import HTMLParser

import pytest
import pytest_asyncio
from fasthtml.common import Div, to_xml
from httpx import ASGITransport, AsyncClient

_SHIELD_LINKS = {
    ("icon", "/static/favicon.ico", "image/x-icon"),
    ("icon", "/static/favicon-32x32.png", "image/png"),
    ("apple-touch-icon", "/static/apple-touch-icon.png", "image/png"),
}

_ICO_TYPES = {"image/x-icon", "image/vnd.microsoft.icon"}


class _IconLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: set[tuple[str, str]] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "link" and a.get("rel") in {"icon", "apple-touch-icon"}:
            self.links.add((a["rel"], a.get("href", "")))


def _icon_links(html: str) -> set[tuple[str, str]]:
    parser = _IconLinks()
    parser.feed(html)
    return parser.links


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
        yield c


def test_app_shell_links_shield_icons():
    from ui.components.shell import minimal_shell
    expected = {(rel, href) for rel, href, _ in _SHIELD_LINKS}
    assert _icon_links(to_xml(minimal_shell(Div()))) == expected


@pytest.mark.asyncio
async def test_auth_page_links_shield_icons(ui_client):
    r = await ui_client.get("/login")
    assert r.status_code == 200
    assert _icon_links(r.text) == {(rel, href) for rel, href, _ in _SHIELD_LINKS}


@pytest.mark.asyncio
@pytest.mark.parametrize("rel,href,content_type", sorted(_SHIELD_LINKS))
async def test_linked_icon_is_served(ui_client, rel, href, content_type):
    r = await ui_client.get(href)
    assert r.status_code == 200
    # The .ico type is registered under either name depending on the platform's
    # mime table; both are what browsers accept for a favicon.
    accepted = _ICO_TYPES if content_type == "image/x-icon" else {content_type}
    assert r.headers["content-type"].split(";")[0] in accepted
    assert r.content
