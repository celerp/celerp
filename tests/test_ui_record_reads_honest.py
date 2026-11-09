"""A UI route never shows a made-up record in place of one the API refused or failed to read.

The census below finds every handler in ui/routes that catches an API failure around a read
fed one of the route's own parameters (the record the page is about) and falls back to an
empty dict or list. Such a fallback renders a blank record with its actions offered, so a
refusal reads as data. Those reads let the failure reach the app's handler instead: a 403
lands on the dashboard's no-access notice, a 404 on the not-found page, anything else on the
error page. The only handlers allowed are the ones listed here, which show the caught error.

Run as a script to print the census rows.
"""
from __future__ import annotations

import ast
import pathlib
import sys
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


ROUTES = pathlib.Path(__file__).resolve().parent.parent / "ui" / "routes"

# Parameters that are not a record: the request itself and credentials.
_NOT_A_RECORD = {"request", "req", "token", "access_token", "lang"}

# Handlers that keep an empty default but put the caught error on the screen beside it.
SHOWS_THE_ERROR = {
    "migrations.py::_xero_attachments": "the fetch error is shown as the step's error text",
    "settings_payments.py::unmatched_invoice_record": "the record refusal is shown in the cell; the picker reloads beside it",
}


def _empty(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.List):
        return not node.elts
    if isinstance(node, ast.Tuple):
        return any(_empty(e) for e in node.elts)
    return False


def _catches_api_failure(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    caught = ast.unparse(handler.type)
    return "APIError" in caught or "Exception" in caught


def census() -> list[tuple[str, int, str, str]]:
    """(file, line, function, api call) for every swallowing handler around a record read."""
    rows = []
    for path in sorted(ROUTES.glob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

        def enclosing_function(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            func = enclosing_function(node)
            if func is None:
                continue
            params = {a.arg for a in func.args.args + func.args.kwonlyargs} - _NOT_A_RECORD
            body = ast.Module(body=node.body, type_ignores=[])
            reads = [
                ast.unparse(call.func) for call in ast.walk(body)
                if isinstance(call, ast.Call) and ast.unparse(call.func).startswith("api.")
                and any(isinstance(x, ast.Name) and x.id in params
                        for arg in call.args + [k.value for k in call.keywords] for x in ast.walk(arg))
            ]
            if not reads:
                continue
            for handler in node.handlers:
                if _catches_api_failure(handler) and any(
                        isinstance(s, ast.Assign) and _empty(s.value) for s in ast.walk(handler)):
                    rows.append((path.name, handler.lineno, func.name, reads[0]))
    return rows


def test_no_route_fabricates_its_record_from_a_failed_read():
    swallowing = sorted({f"{name}::{func}" for name, _line, func, _call in census()} - set(SHOWS_THE_ERROR))
    assert swallowing == [], (
        "These routes fall back to an empty record when the API refuses or fails to read it. "
        "Let the failure reach the app's handler (403 no-access, 404 not found, else error page): "
        + ", ".join(swallowing))


def test_allowed_handlers_still_exist():
    """The allowlist only shrinks: an entry whose handler is gone is removed, not kept."""
    found = {f"{name}::{func}" for name, _line, func, _call in census()}
    assert set(SHOWS_THE_ERROR) <= found


# ── Behaviour: what a refused or failed read shows ────────────────────────────

@pytest_asyncio.fixture
async def ui_client():
    from test_helpers import make_test_token
    from ui.app import app as ui_app
    # The 500 page is served by the app's error middleware, which re-raises to the
    # transport after answering; the test reads the answer the browser gets.
    async with AsyncClient(transport=ASGITransport(app=ui_app, raise_app_exceptions=False), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token(role="owner")})
        yield c


_COMPANY = {"id": "c1", "name": "Co", "current_role": "owner", "settings": {}, "currency": "THB"}

# One door per kind of read: a List page, a contact tab, a contact fragment, a list page
# filtered by a parameter, and an item's history.
_DOORS = [
    ("/lists/list:WO-2611-0002", "get_list_page"),
    ("/contacts/contact:1/tab/notes", "list_contact_notes"),
    ("/contacts/contact:1/addresses/section", "get_contact"),
    ("/subscriptions?direction=sales", "list_subscriptions"),
    ("/inventory/item:1/history", "get_item"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path,read", _DOORS)
async def test_refused_read_lands_on_the_no_access_notice(ui_client, path, read):
    from ui.api_client import APIError
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch(f"ui.api_client.{read}", new=AsyncMock(side_effect=APIError(403, "Requires view_documents"))):
        r = await ui_client.get(path)
    assert r.status_code == 302, r.text[:300]
    assert r.headers["location"] == "/dashboard"
    assert "celerp_notice=not_permitted" in r.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_refused_fragment_navigates_to_the_no_access_notice(ui_client):
    from ui.api_client import APIError
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.list_contact_notes", new=AsyncMock(side_effect=APIError(403, "no"))):
        r = await ui_client.get("/contacts/contact:1/tab/notes", headers={"HX-Request": "true"})
    assert r.headers.get("hx-redirect") == "/dashboard"


@pytest.mark.asyncio
async def test_failed_list_read_shows_the_error_page_not_a_blank_list(ui_client):
    from ui.api_client import APIError
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_list_page", new=AsyncMock(side_effect=APIError(500, "boom"))):
        r = await ui_client.get("/lists/list:WO-2611-0002")
    assert r.status_code == 500
    assert "No line items" not in r.text and "Duplicate" not in r.text


@pytest.mark.asyncio
async def test_missing_record_shows_not_found(ui_client):
    from ui.api_client import APIError
    with patch("ui.api_client.get_company", new=AsyncMock(return_value=_COMPANY)), \
         patch("ui.api_client.get_contact", new=AsyncMock(side_effect=APIError(404, "Not found"))):
        r = await ui_client.get("/contacts/contact:1/addresses/section")
    assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("entity_id", ["%20", "%09"])
async def test_history_of_a_blank_id_is_not_the_company_ledger(ui_client, entity_id):
    ledger = AsyncMock(return_value={"items": [{"id": 1}], "total": 306})
    with patch("ui.api_client.list_ledger", new=ledger):
        r = await ui_client.get(f"/docs/{entity_id}/history")
    assert r.status_code == 404
    ledger.assert_not_called()


if __name__ == "__main__":
    for row in census():
        print("\t".join(map(str, row)))
    sys.exit(0)
