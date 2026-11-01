# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every share link writer takes the company's lock before the share token row: creating,
re-sharing and revoking a link. Two first shares of a document then mint one token, and the
lock checker's census finds no token taken outside the company lock."""

from __future__ import annotations

import sys


def _ungated() -> set[str]:
    return sys.modules["lock_rank"].RECORDER.ungated_sites({"row:doc_share_tokens"}, "celerp_docs/")


async def test_sharing_and_revoking_a_link_take_the_company_lock_first(client, auth):
    before = _ungated()
    h = auth["headers"]
    r = await client.post("/docs", headers=h, json={
        "doc_type": "invoice", "contact_name": "ACME",
        "line_items": [{"description": "Widget", "quantity": 1, "unit_price": 10.0}]})
    assert r.status_code == 200, r.text
    entity_id = r.json()["id"]
    for _ in range(2):
        r = await client.post(f"/docs/{entity_id}/share", headers=h)
        assert r.status_code == 200, r.text
        r = await client.delete(f"/docs/{entity_id}/share", headers=h)
        assert r.status_code == 200, r.text
    assert _ungated() - before == set()
