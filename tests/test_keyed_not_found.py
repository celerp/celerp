"""A 404 raised by the app keeps its detail: a keyed refusal reaches the reader with its
message key, so it renders in their language, and a plain detail keeps its own words.
A path no route matches still answers 404."""
from __future__ import annotations

import uuid

import pytest

from test_helpers import in_language
from ui.i18n import t

pytestmark = pytest.mark.asyncio


async def test_keyed_not_found_keeps_its_message_key(client, auth):
    r = await client.post("/accounting/older-stock/move", headers=auth["headers"],
                          json={"item_ids": [f"item:{uuid.uuid4()}"], "code": "1130-P", "key": "k"})
    assert r.status_code == 404, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["message_key"] == "posting.older_stock.no_item"
    assert in_language("de", detail) != detail["message"]


async def test_plain_not_found_keeps_its_words(client, auth):
    r = await client.patch(f"/companies/me/locations/{uuid.uuid4()}", headers=auth["headers"], json={"name": "X"})
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == t("company.err_location_not_found", "en")


async def test_unmatched_path_still_answers_404(client, auth):
    r = await client.get("/no-such-route-anywhere", headers=auth["headers"])
    assert r.status_code == 404
