# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every label template writer takes the company's lock before the template row: seeding the
presets and a source's template on first read, and creating, changing and deleting a template.
Two first reads then seed once, and the lock checker's census finds no template taken outside
the company lock."""

from __future__ import annotations

import sys


def _ungated() -> set[str]:
    return sys.modules["lock_rank"].RECORDER.ungated_sites({"row:label_templates"}, "celerp_labels/")


async def test_seeding_and_editing_templates_take_the_company_lock_first(client, session, auth, ids):
    from celerp_labels.routes import _source_templates

    before = _ungated()
    h = auth["headers"]
    r = await client.get("/api/labels/templates", headers=h)
    assert r.status_code == 200, r.text
    await _source_templates(session, ids["company_id"], {
        "entity_type": "x_widget", "label_key": "label.name", "keys": ["name"],
        "preset": {"fields": [{"key": "name", "label": "label.name", "type": "text"}]}})
    r = await client.post("/api/labels/templates", headers=h, json={"name": "Shelf", "format": "40x30mm"})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await client.put(f"/api/labels/templates/{tid}", headers=h, json={"name": "Shelf tag"})
    assert r.status_code == 200, r.text
    r = await client.delete(f"/api/labels/templates/{tid}", headers=h)
    assert r.status_code == 204, r.text
    assert _ungated() - before == set()
