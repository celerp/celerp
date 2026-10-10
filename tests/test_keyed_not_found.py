"""A 404 raised by the app keeps its detail: a keyed refusal reaches the reader with its
message key, so it renders in their language, and a plain detail keeps its own words.
A path no route matches still answers 404."""
from __future__ import annotations

import uuid

import pytest

from test_helpers import in_language
from test_posting_accounts_panel import _account, _put
from test_posting_roles_older_stock import _choose

pytestmark = pytest.mark.asyncio


async def test_keyed_not_found_keeps_its_message_key(client, auth):
    await _account(client, auth, "1135", "asset", "1100")
    assert (await _put(client, auth, "inventory_purchased", "1135")).status_code == 200
    r = await _choose(client, auth, f"item:{uuid.uuid4()}", "1135")
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["message_key"] == "posting.older_stock.no_item"
    assert in_language("de", detail) != detail["message"]


async def test_plain_not_found_keeps_its_words(client, auth):
    r = await client.patch(f"/companies/me/locations/{uuid.uuid4()}", headers=auth["headers"], json={"name": "X"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Location not found"


async def test_unmatched_path_still_answers_404(client, auth):
    r = await client.get("/no-such-route-anywhere", headers=auth["headers"])
    assert r.status_code == 404
