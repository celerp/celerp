# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The fund controls on a document page in a company with funds in use: "Fund for this
document" in the header of a record that came from outside, a Fund cell on each line, and
"Set all outside lines to" above the lines while a line from outside has no fund to post to.
The API is mocked; the writes themselves are tested in test_outside_lines."""
from __future__ import annotations

import os
import re

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token
from test_ui import _company_stub, _mock_get_company  # noqa: F401  (autouse fixture: the company settings)

pytestmark = pytest.mark.asyncio

_FUNDS = {"items": [{"id": "fund:r", "name": "Building fund", "archived": False},
                    {"id": "fund:old", "name": "Old appeal", "archived": True}]}
_FROM = "From a connected company"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


def _bill(*, status: str = "draft", choice: str | None = None, line_choice: str | None = None,
          local_fund: str | None = None) -> dict:
    marked = {"line_id": "L1", "description": "Paper", "quantity": 1, "unit_price": 40.0, "line_total": 40.0,
              "origin": "connected_company", **({"fund_choice": line_choice} if line_choice else {})}
    local = {"line_id": "L2", "description": "Delivery", "quantity": 1, "unit_price": 10.0, "line_total": 10.0,
             **({"fund_id": local_fund} if local_fund else {})}
    return {"entity_id": "doc:b1", "doc_type": "bill", "status": status, "currency": "USD", "version": 7,
            "origin": "connected_company", **({"fund_choice": choice} if choice else {}),
            "line_items": [marked, local], "total": 50.0}


async def _page(ui_client, doc: dict, *, settings: dict | None = None) -> str:
    company = {"id": "c1", "name": "Test Corp", "currency": "USD", "timezone": "UTC", "fiscal_year_start": "01-01",
               "settings": {"funds_enabled": True} if settings is None else settings}
    with patch("ui.api_client.get_doc", new=AsyncMock(return_value=doc)), \
         patch("ui.api_client.list_funds", new=AsyncMock(return_value=_FUNDS)), \
         patch("ui.api_client.get_company", new=AsyncMock(side_effect=_company_stub(company))):
        r = await ui_client.get(f"/docs/{doc['entity_id']}")
    assert r.status_code == 200, r.text
    return r.text


def _element(html: str, dom_id: str) -> str | None:
    """The text inside the element with ``dom_id`` (tags dropped), or None when absent."""
    found = re.search(rf'<(\w+)[^>]*id="{re.escape(dom_id)}"[^>]*>(.*?)</\1>', html, re.S)
    return " ".join(re.sub(r"<[^>]+>", " ", found.group(2)).split()) if found else None


async def test_document_fund_cell_starts_unset_for_outside_lines(ui_client):
    html = await _page(ui_client, _bill())
    marked, local = _element(html, "fund-cell-L1"), _element(html, "fund-cell-L2")
    assert marked is not None and "--" in marked and _FROM in marked, marked
    assert local == "Unrestricted", local
    assert 'class="badge badge--origin"' in html
    header = _element(html, "doc-fund")
    assert header is not None and "--" in header and _FROM in header, header
    assert "Set all outside lines to" in (_element(html, "fund-set-all") or ""), "Set all missing"
    finalize = re.search(r"<button[^>]*/docs/doc:b1/action/finalize[^>]*>", html)
    assert finalize and "disabled" not in finalize.group(0), "Finalize must stay enabled"
    # Only active funds are offered, after Unrestricted.
    assert "Old appeal" not in html

    # The line's own choice reads as its fund; nothing is left waiting, so Set all goes.
    chosen = await _page(ui_client, _bill(line_choice="fund:r"))
    assert "Building fund" in _element(chosen, "fund-cell-L1")
    assert _element(chosen, "fund-set-all") is None

    # The document's choice covers the line, which reads -- and, muted, the fund it uses.
    covered = await _page(ui_client, _bill(choice="fund:r"))
    cell = _element(covered, "fund-cell-L1")
    assert "--" in cell and "uses Building fund" in cell, cell
    assert "Building fund" in _element(covered, "doc-fund")
    assert _element(covered, "fund-set-all") is None

    # A local line shows the fund it carries.
    assert _element(await _page(ui_client, _bill(local_fund="fund:r")), "fund-cell-L2") == "Building fund"


async def test_fund_controls_absent_without_funds_or_outside_lines(ui_client):
    off = await _page(ui_client, _bill(), settings={})
    assert _element(off, "fund-cell-L1") is None and _element(off, "doc-fund") is None
    local_only = _bill()
    local_only.pop("origin")
    local_only["line_items"] = [local_only["line_items"][1]]
    html = await _page(ui_client, local_only)
    assert _element(html, "doc-fund") is None and _element(html, "fund-cell-L2") is None


async def test_posted_document_fund_cells_read_only(ui_client):
    html = await _page(ui_client, _bill(status="final", line_choice="fund:r"))
    assert "Building fund" in _element(html, "fund-cell-L1")
    assert not re.search(r'id="fund-cell-L1"[^>]*hx-get', html)
    assert not re.search(r'id="doc-fund"[^>]*hx-get', html)
