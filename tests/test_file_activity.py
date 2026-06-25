# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""M6 — file events in the activity log carry the filename and render as a link.

Backend: attach / tag / describe / set-hero / delete a file and assert each
item.file.* ledger event captures the filename.
Render: activity_table shows the nice labels + the filename linked to the file's
download URL, with no live link once the file is deleted.
"""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from ui.components.activity import activity_table


async def _token(client) -> str:
    r = await client.post(
        "/auth/register",
        json={"company_name": "Files Co", "email": "files@acme.com", "name": "Admin", "password": "pw"},
    )
    return r.json()["access_token"]


async def _seed(client, headers) -> str:
    r = await client.post(
        "/items",
        json={"sku": "FILESKU", "name": "Has Files", "quantity": 1, "sell_by": "piece"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_file_events_capture_filename(client):
    h = {"Authorization": f"Bearer {await _token(client)}"}
    item_id = await _seed(client, h)

    r = await client.post(
        f"/items/{item_id}/files",
        files={"file": ("photo.jpg", b"\xff\xd8\xff\xe0image-bytes", "image/jpeg")},
        headers=h,
    )
    assert r.status_code == 200, r.text
    file_id = r.json()["file_id"]

    assert (await client.post(f"/items/{item_id}/files/{file_id}/tag",
                              data={"document_tag": "certificates"}, headers=h)).status_code == 200
    assert (await client.patch(f"/items/{item_id}/files/{file_id}/description",
                               data={"description": "front view"}, headers=h)).status_code == 200
    assert (await client.post(f"/items/{item_id}/files/{file_id}/hero", headers=h)).status_code == 200
    assert (await client.delete(f"/items/{item_id}/files/{file_id}", headers=h)).status_code == 204

    r = await client.get("/ledger", params={"entity_id": item_id, "limit": 200}, headers=h)
    assert r.status_code == 200, r.text
    by_type: dict[str, list[dict]] = {}
    for e in r.json()["items"]:
        by_type.setdefault(e["event_type"], []).append(e)

    fname = by_type["item.file.attached"][0]["data"].get("filename")
    assert fname, "attached event must carry the filename"
    for et in ("item.file.tagged", "item.file.description_updated",
               "item.file.hero_set", "item.file.deleted"):
        assert et in by_type, f"missing event {et}"
        assert by_type[et][0]["data"].get("filename") == fname, f"{et} did not capture the filename"


# file event types per entity (attached / tagged / description / deleted) — the renderer
# handles all three identically, keyed by entity_type for the download URL segment.
_FILE_EVENTS = {
    "item": ("item.file.attached", "item.file.tagged",
             "item.file.description_updated", "item.file.deleted"),
    "contact": ("crm.contact.file_attached", "crm.contact.file_tagged",
                "crm.contact.file_description_updated", "crm.contact.file_deleted"),
    "doc": ("doc.file_attached", "doc.file_tagged",
            "doc.file_description_updated", "doc.file_deleted"),
}
_ENTITY_SEG = {"item": "items", "contact": "contacts", "doc": "docs"}


def _file_entry(event_type: str, entity_type: str, entity_id: str, file_id: str, **extra) -> dict:
    return {
        "id": f"e-{event_type}",
        "event_type": event_type,
        "entity_id": entity_id,
        "ts": "2026-06-23T00:00:00+00:00",
        "data": {"entity_id": entity_id, "entity_type": entity_type,
                 "file_id": file_id, "filename": "photo.jpg", **extra},
    }


@pytest.mark.parametrize("entity_type", ["item", "contact", "doc"])
def test_file_event_activity_render_labels_and_links(entity_type):
    attached, tagged, described, deleted = _FILE_EVENTS[entity_type]
    seg, entity_id, file_id = _ENTITY_SEG[entity_type], f"{entity_type}:abc", "file-123"
    ledger = [
        _file_entry(attached, entity_type, entity_id, file_id),
        _file_entry(tagged, entity_type, entity_id, file_id, document_tag="certificates"),
        _file_entry(described, entity_type, entity_id, file_id, description="front view"),
        _file_entry(deleted, entity_type, entity_id, file_id),
    ]
    html = to_xml(activity_table(ledger, max_display=50))
    for label in ("File attached", "File tagged", "File description updated", "File removed"):
        assert label in html, f"{entity_type}: missing label {label}"
    assert "photo.jpg" in html
    assert f"/{seg}/{entity_id}/files/{file_id}/download" in html, f"{entity_type}: missing download link"


@pytest.mark.parametrize("entity_type", ["item", "contact", "doc"])
def test_deleted_file_event_has_no_live_link(entity_type):
    _, _, _, deleted = _FILE_EVENTS[entity_type]
    entity_id, file_id = f"{entity_type}:abc", "file-123"
    html = to_xml(activity_table([_file_entry(deleted, entity_type, entity_id, file_id)], max_display=10))
    assert "photo.jpg" in html, f"{entity_type}: deleted event should still name the file"
    assert "/download" not in html, f"{entity_type}: a deleted file must not render a live link"


def test_item_hero_event_label():
    html = to_xml(activity_table([_file_entry("item.file.hero_set", "item", "item:abc", "f1")], max_display=10))
    assert "Hero image set" in html