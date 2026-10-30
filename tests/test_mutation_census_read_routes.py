# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""A read route no test requests is judged from its source: the census fails it when its
handler reaches a write through any number of calls, a mutating API request from the UI
included, unless the write is made only through a function READ_SAFE names or a function a
READ_ROUTE_WRITES note lets that route write through."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import mutation_census  # noqa: E402

CLIENT = '''\
async def create_list(token, data):
    async with _client(token) as c:
        return await c.post("/lists", json=data)


async def items_metadata(token, ids):
    async with _client(token) as c:
        return await c.post("/items/metadata", json={"ids": ids})
'''

ROUTES = '''\
import ui.api_client as api


def setup_routes(app):
    @app.get("/lists/new")
    async def lists_new(request):
        await _open(request)

    async def _open(request):
        await api.create_list("t", {})

    @app.get("/docs/{id}")
    async def doc(request, id):
        await api.items_metadata("t", [id])
'''


def _scan(tmp_path, monkeypatch, safe=()):
    (tmp_path / "ui" / "routes").mkdir(parents=True)
    (tmp_path / "ui" / "api_client.py").write_text(CLIENT)
    (tmp_path / "ui" / "routes" / "x.py").write_text(ROUTES)
    monkeypatch.setattr(mutation_census.static_calls, "ROOT", str(tmp_path))
    monkeypatch.setattr(mutation_census, "READ_SAFE", dict.fromkeys(safe, "reads"))
    rows = {("FastHTML", "GET", "/lists/new"): {"source": "ui/routes/x.py:5"},
            ("FastHTML", "GET", "/docs/{id}"): {"source": "ui/routes/x.py:12"}}
    found = mutation_census.routes_that_write(rows, mutation_census.static_calls.Graph())
    return {path: [mutation_census.static_calls.describe(c) for c in chains] for (_, _, path), chains in found.items()}


def test_a_read_route_reaching_a_mutating_request_through_a_helper_is_found(tmp_path, monkeypatch):
    found = _scan(tmp_path, monkeypatch)
    assert found["/lists/new"] == ["lists_new:7 _open -> _open:10 create_list -> create_list:3 api POST"]
    assert found["/docs/{id}"] == ["doc:14 items_metadata -> items_metadata:8 api POST"]


def test_a_write_made_only_through_a_read_safe_function_is_not_found(tmp_path, monkeypatch):
    assert list(_scan(tmp_path, monkeypatch, safe=["ui.api_client.items_metadata"])) == ["/lists/new"]


def _noted(monkeypatch, route):
    notes = mutation_census.mutation_read_notes
    monkeypatch.setattr(notes, "READ_ROUTE_WRITES", {("items", "UPDATE"): notes.ReadNote(
        ("seen_at",), ("ui/api_client.py items_metadata",), "a note of the read", route=route)})


def test_a_write_made_only_through_a_read_note_site_for_the_route_is_not_found(tmp_path, monkeypatch):
    _noted(monkeypatch, "GET /docs/{id}")
    assert list(_scan(tmp_path, monkeypatch)) == ["/lists/new"]


def test_a_read_note_scoped_to_another_route_does_not_cover_the_site(tmp_path, monkeypatch):
    _noted(monkeypatch, "GET /other")
    assert sorted(_scan(tmp_path, monkeypatch)) == ["/docs/{id}", "/lists/new"]
