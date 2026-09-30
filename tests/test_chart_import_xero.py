# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A Xero chart of accounts export, imported through the browser chart import
and written by the real accounting API.

The file goes through every step a user takes: upload, map Xero's columns,
fix the rows whose type Xero names differently, confirm. Nothing is mocked
between the UI and the database, so these tests prove the accounts persist,
an account already in the chart is kept as it was, parents resolve whatever
order the rows are in, running the same file again adds nothing, and a file
with broken parents says which rows and why.
"""

from __future__ import annotations

import csv
import io
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from celerp.importers.tabular import MAPPING_SKIP

_FIXTURES = Path(__file__).parent / "fixtures" / "xero"
_XERO = _FIXTURES / "ChartOfAccounts.csv"
_XERO_WITH_PARENTS = _FIXTURES / "ChartOfAccounts_with_parents.csv"

# What a user picks in the fix step for each account type Xero writes.
_TYPE_FOR_XERO = {
    "Bank": "asset", "Current Asset": "asset", "Accounts Receivable": "asset",
    "Inventory": "asset", "Fixed Asset": "asset",
    "Accounts Payable": "liability", "GST": "liability", "Non-current Liability": "liability",
    "Equity": "equity", "Retained Earnings": "equity",
    "Revenue": "revenue", "Direct Costs": "cogs", "Expense": "expense",
}

_MAPPING = {"*Code": "code", "*Name": "name", "*Type": "account_type", "Parent Code": "parent_code"}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Keep browser import stages inside the test's own directory."""
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path


@pytest.fixture
async def owner(client, data_dir):
    r = await client.post("/auth/register", json={
        "company_name": "Xero Move Co", "email": "owner@xero-move.test", "name": "Owner", "password": "validpass1"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"token": token, "h": {"Authorization": f"Bearer {token}"}}


@asynccontextmanager
async def _browser():
    """The real UI app, with its API client routed to the in-process API."""
    from celerp.main import app as api_app
    from ui.app import app as ui_app

    def _bridged(token=None, *, timeout=10.0, follow_redirects=True, bulk=False, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        return AsyncClient(
            transport=ASGITransport(app=api_app), base_url="http://test",
            headers=merged, timeout=timeout, follow_redirects=follow_redirects,
        )

    with patch("ui.api_client._local_client", _bridged):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            yield c


def _csv_ref(html: str) -> str:
    m = re.search(r'name="csv_ref" value="([^"]+)"', html)
    assert m, html[:2000]
    return m.group(1)


def _reversed(path: Path) -> bytes:
    """The same file with its data rows in the opposite order."""
    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows([rows[0], *reversed(rows[1:])])
    return out.getvalue().encode()


async def _import_file(owner: dict, content: bytes, filename: str = "ChartOfAccounts.csv") -> tuple[str, str, str]:
    """Upload, map, fix types and confirm. Returns (fix-step page, result page, confirmed stage ref)."""
    cookies = {"celerp_token": owner["token"]}
    async with _browser() as ui:
        r = await ui.post("/accounting/import/chart/preview", cookies=cookies,
                          files={"csv_file": (filename, content, "text/csv")})
        assert r.status_code == 200, r.text
        header = next(csv.reader(io.StringIO(content.decode())))
        form = {f"map__{col}": _MAPPING.get(col, MAPPING_SKIP) for col in header}
        r = await ui.post("/accounting/import/chart/mapped", cookies=cookies,
                          data={"csv_ref": _csv_ref(r.text), **form})
        assert r.status_code == 200, r.text
        fix_page = r.text

        rows = list(csv.DictReader(io.StringIO(content.decode())))
        fixes = {f"{i}__account_type": _TYPE_FOR_XERO[row["*Type"]] for i, row in enumerate(rows)}
        r = await ui.post("/accounting/import/chart/revalidate", cookies=cookies,
                          data={"csv_ref": _csv_ref(fix_page), "fixes_json": json.dumps(fixes)})
        assert r.status_code == 200, r.text
        assert 'hx-post="/accounting/import/chart/revalidate"' not in r.text, "rows still invalid after fixes"

        ref = _csv_ref(r.text)
        r = await ui.post("/accounting/import/chart/confirm", cookies=cookies, data={"csv_ref": ref})
        assert r.status_code == 200, r.text
        return fix_page, r.text, ref


async def _chart(client, owner) -> dict[str, dict]:
    r = await client.get("/accounting/chart", headers=owner["h"])
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["items"]}


def _cards(html: str) -> dict[str, int]:
    """The result panel's summary cards, label to number."""
    pairs = re.findall(r'import-card-value">(\d+)</div>\s*<div class="import-card-label">([^<]+)<', html)
    return {label.strip(): int(value) for value, label in pairs}


def _xero_rows(path: Path = _XERO) -> list[dict]:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))))


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xero_chart_file_imports_through_real_writer(client, owner):
    before = await _chart(client, owner)
    fix_page, result, _ = await _import_file(owner, _XERO.read_bytes())

    # Xero's own type names are not account types here; the fix step says so.
    assert 'hx-post="/accounting/import/chart/revalidate"' in fix_page

    xero = _xero_rows()
    assert _cards(result).get("Created") == len(xero), result
    chart = await _chart(client, owner)
    assert set(chart) - set(before) == {r["*Code"] for r in xero}
    for r in xero:
        acc = chart[r["*Code"]]
        assert acc["name"] == r["*Name"]
        assert acc["account_type"] == _TYPE_FOR_XERO[r["*Type"]]
        assert acc["is_active"] is True and acc["parent_code"] is None


@pytest.mark.asyncio
async def test_xero_chart_file_retry_creates_nothing(client, owner):
    await _import_file(owner, _XERO.read_bytes())
    chart = await _chart(client, owner)
    _, result, _ = await _import_file(owner, _XERO.read_bytes())
    cards = _cards(result)
    assert cards.get("Created") == 0 and cards.get("Skipped") == len(_xero_rows()), result
    assert await _chart(client, owner) == chart


@pytest.mark.asyncio
async def test_xero_chart_file_keeps_existing_accounts_unchanged(client, owner):
    r = await client.post("/accounting/accounts", headers=owner["h"], json={
        "code": "200", "name": "Shop Takings", "account_type": "revenue"})
    assert r.status_code == 200, r.text
    before = (await _chart(client, owner))["200"]

    _, result, _ = await _import_file(owner, _XERO.read_bytes())

    assert (await _chart(client, owner))["200"] == before
    cards = _cards(result)
    assert cards.get("Created") == len(_xero_rows()) - 1 and cards.get("Skipped") == 1, result
    assert "Existing codes are kept." in result and "Skipped: 200" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["file", "reversed"])
async def test_chart_file_parents_resolve_in_any_row_order(client, owner, order):
    content = _XERO_WITH_PARENTS.read_bytes() if order == "file" else _reversed(_XERO_WITH_PARENTS)
    _, result, _ = await _import_file(owner, content)

    rows = _xero_rows(_XERO_WITH_PARENTS)
    assert _cards(result).get("Created") == len(rows), result
    chart = await _chart(client, owner)
    for r in rows:
        assert chart[r["*Code"]]["parent_code"] == (r["Parent Code"] or None), r["*Code"]


@pytest.mark.asyncio
async def test_chart_file_invalid_parents_and_cycles_are_shown_and_stage_kept(client, owner, data_dir):
    content = (
        "*Code,*Name,*Type,Parent Code\n"
        "500,Orphan,Expense,999\n"
        "510,Loop A,Expense,520\n"
        "520,Loop B,Expense,510\n"
        "530,Fine,Expense,\n"
    ).encode()
    _, result, ref = await _import_file(owner, content)

    cards = _cards(result)
    assert cards.get("Created") == 1 and cards.get("Errors") == 3, result
    assert "500" in result and "999" in result
    assert result.count("loop") >= 2
    chart = await _chart(client, owner)
    assert "530" in chart and not {"500", "510", "520"} & set(chart)
    # The confirmed stage stays so the user can go back, fix the file and retry.
    assert (data_dir / "import_staging" / f"{ref}.csv").exists(), "stage was discarded despite row errors"


@pytest.mark.asyncio
async def test_chart_file_of_more_than_500_accounts_imports_in_one_go(client, owner):
    content = "*Code,*Name,*Type,Parent Code\n" + "".join(
        f"X{i:04d},Account {i},Expense,{'X0000' if i else ''}\n" for i in range(600))
    _, result, _ = await _import_file(owner, content.encode())
    assert _cards(result).get("Created") == 600, result[:2000]
    chart = await _chart(client, owner)
    assert chart["X0599"]["parent_code"] == "X0000"


@pytest.mark.asyncio
async def test_chart_import_page_shows_add_only_copy(client, owner):
    async with _browser() as ui:
        r = await ui.get("/accounting/import/chart", cookies={"celerp_token": owner["token"]})
    assert r.status_code == 200, r.text
    assert "Adds accounts to your chart. Existing codes are kept." in r.text
