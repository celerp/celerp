# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for item.file.* projection events and lazy attachment migration."""

from __future__ import annotations

import pytest

from celerp_inventory.projections import apply_item_event, _maybe_migrate_attachments


def _base_state() -> dict:
    return apply_item_event({}, "item.created", {"sku": "T1", "name": "Test", "quantity": 1})


def _file_attached(state: dict, file_id: str = "f1", mime: str = "image/jpeg",
                   tag: str | None = "product_images", is_hero: bool = False) -> dict:
    return apply_item_event(state, "item.file.attached", {
        "entity_id": "item:test",
        "entity_type": "item",
        "file_id": file_id,
        "filename": f"{file_id}.jpg",
        "mime": mime,
        "size": 1024,
        "url": f"/static/attachments/co/{file_id}.jpg",
        "document_tag": tag,
        "description": None,
        "uploaded_at": "2026-05-19T00:00:00Z",
        "is_hero": is_hero,
    })


class TestItemFileAttached:
    def test_file_appears_in_files_list(self):
        state = _base_state()
        state = _file_attached(state, "f1", is_hero=True)
        assert len(state["files"]) == 1
        assert state["files"][0]["id"] == "f1"

    def test_hero_flag_set(self):
        state = _base_state()
        state = _file_attached(state, "f1", is_hero=True)
        assert state["files"][0]["is_hero"] is True
        assert state["preview_image_id"] == "f1"

    def test_new_hero_clears_old_hero(self):
        state = _base_state()
        state = _file_attached(state, "f1", is_hero=True)
        state = _file_attached(state, "f2", is_hero=True)
        assert state["files"][0]["is_hero"] is False
        assert state["files"][1]["is_hero"] is True
        assert state["preview_image_id"] == "f2"

    def test_non_hero_does_not_touch_preview(self):
        state = _base_state()
        state = _file_attached(state, "f1", is_hero=True)
        state = _file_attached(state, "f2", is_hero=False)
        assert state["preview_image_id"] == "f1"

    def test_pdf_file_stored_correctly(self):
        state = _base_state()
        state = apply_item_event(state, "item.file.attached", {
            "entity_id": "item:test", "entity_type": "item",
            "file_id": "cert1", "filename": "cert.pdf",
            "mime": "application/pdf", "size": 4096,
            "url": "/static/attachments/co/cert1.pdf",
            "document_tag": "certificates", "description": None,
            "uploaded_at": "2026-05-19T00:00:00Z", "is_hero": False,
        })
        f = state["files"][0]
        assert f["document_tag"] == "certificates"
        assert f["is_hero"] is False
        assert state.get("preview_image_id") is None


class TestItemFileTagged:
    def test_tag_updated(self):
        state = _base_state()
        state = _file_attached(state, "f1", tag="product_images")
        state = apply_item_event(state, "item.file.tagged", {
            "entity_id": "item:test", "entity_type": "item",
            "file_id": "f1", "document_tag": "certificates",
        })
        assert state["files"][0]["document_tag"] == "certificates"

    def test_unknown_file_id_no_error(self):
        state = _base_state()
        state = _file_attached(state, "f1")
        # Should not raise; unknown id → no change
        state = apply_item_event(state, "item.file.tagged", {
            "entity_id": "item:test", "entity_type": "item",
            "file_id": "nonexistent", "document_tag": "certificates",
        })
        assert state["files"][0]["document_tag"] == "product_images"


class TestItemFileDescriptionUpdated:
    def test_description_set(self):
        state = _base_state()
        state = _file_attached(state, "f1")
        state = apply_item_event(state, "item.file.description_updated", {
            "entity_id": "item:test", "entity_type": "item",
            "file_id": "f1", "description": "Front view",
        })
        assert state["files"][0]["description"] == "Front view"


class TestItemFileDeleted:
    def test_file_removed(self):
        state = _base_state()
        state = _file_attached(state, "f1")
        state = apply_item_event(state, "item.file.deleted", {
            "entity_id": "item:test", "entity_type": "item", "file_id": "f1",
        })
        assert state["files"] == []

    def test_hero_deleted_auto_promotes_next_image(self):
        state = _base_state()
        state = _file_attached(state, "f1", mime="image/jpeg", is_hero=True)
        state = _file_attached(state, "f2", mime="image/jpeg", is_hero=False)
        state = apply_item_event(state, "item.file.deleted", {
            "entity_id": "item:test", "entity_type": "item", "file_id": "f1",
        })
        assert state["files"][0]["id"] == "f2"
        assert state["files"][0]["is_hero"] is True
        assert state["preview_image_id"] == "f2"

    def test_hero_deleted_no_images_left_clears_preview(self):
        state = _base_state()
        state = _file_attached(state, "f1", mime="image/jpeg", is_hero=True)
        state = apply_item_event(state, "item.file.deleted", {
            "entity_id": "item:test", "entity_type": "item", "file_id": "f1",
        })
        assert state["files"] == []
        assert state.get("preview_image_id") is None

    def test_non_hero_deleted_does_not_change_hero(self):
        state = _base_state()
        state = _file_attached(state, "f1", mime="image/jpeg", is_hero=True)
        state = _file_attached(state, "f2", mime="image/jpeg", is_hero=False)
        state = apply_item_event(state, "item.file.deleted", {
            "entity_id": "item:test", "entity_type": "item", "file_id": "f2",
        })
        assert state["files"][0]["id"] == "f1"
        assert state["files"][0]["is_hero"] is True
        assert state["preview_image_id"] == "f1"


class TestItemFileHeroSet:
    def test_hero_set_clears_others(self):
        state = _base_state()
        state = _file_attached(state, "f1", mime="image/jpeg", is_hero=True)
        state = _file_attached(state, "f2", mime="image/jpeg", is_hero=False)
        state = apply_item_event(state, "item.file.hero_set", {
            "entity_id": "item:test", "entity_type": "item", "file_id": "f2",
        })
        assert state["files"][0]["is_hero"] is False
        assert state["files"][1]["is_hero"] is True
        assert state["preview_image_id"] == "f2"

    def test_hero_set_updates_preview_image_id(self):
        state = _base_state()
        state = _file_attached(state, "f1", mime="image/jpeg", is_hero=False)
        state = apply_item_event(state, "item.file.hero_set", {
            "entity_id": "item:test", "entity_type": "item", "file_id": "f1",
        })
        assert state["preview_image_id"] == "f1"


class TestLazyMigration:
    def test_attachments_migrated_on_first_file_event(self):
        """Old item.state["attachments"] converted to files on first item.file.attached."""
        state = _base_state()
        state["attachments"] = [
            {"id": "old1", "type": "image", "filename": "front.jpg", "mime": "image/jpeg", "size": 1000, "url": "/static/old1.jpg"},
            {"id": "old2", "type": "certificate", "filename": "cert.pdf", "mime": "application/pdf", "size": 2000, "url": "/static/old2.pdf"},
        ]
        state["preview_image_id"] = "old1"
        # Trigger migration by firing a new file event
        state = _file_attached(state, "new1", mime="image/jpeg", is_hero=False)

        # Old attachments migrated
        ids = [f["id"] for f in state["files"]]
        assert "old1" in ids
        assert "old2" in ids
        assert "new1" in ids
        assert state["attachments"] == []

        old1 = next(f for f in state["files"] if f["id"] == "old1")
        old2 = next(f for f in state["files"] if f["id"] == "old2")
        assert old1["document_tag"] == "product_images"
        assert old1["is_hero"] is True  # was preview_image_id
        assert old2["document_tag"] == "certificates"

    def test_migration_idempotent(self):
        """Calling _maybe_migrate_attachments twice does not duplicate files."""
        state = {"attachments": [
            {"id": "a1", "type": "image", "filename": "f.jpg", "mime": "image/jpeg", "size": 100, "url": "/s/f.jpg"}
        ], "files": []}
        _maybe_migrate_attachments(state)
        _maybe_migrate_attachments(state)
        assert len(state["files"]) == 1

    def test_migration_skipped_when_files_already_present(self):
        """If files already exist, old attachments are not touched."""
        state = {
            "attachments": [{"id": "a1", "type": "image", "filename": "old.jpg", "mime": "image/jpeg", "size": 100, "url": "/s/old.jpg"}],
            "files": [{"id": "f1", "filename": "new.jpg", "mime": "image/jpeg", "size": 100, "url": "/s/new.jpg", "document_tag": "product_images", "is_hero": True}],
        }
        _maybe_migrate_attachments(state)
        assert len(state["files"]) == 1  # not touched
        assert state["files"][0]["id"] == "f1"

    def test_view_360_attachment_migrated_to_tag(self):
        state = {"attachments": [
            {"id": "v1", "type": "view_360", "filename": "tour.mp4", "mime": "video/mp4", "size": 5000, "url": "/s/tour.mp4"}
        ], "files": []}
        _maybe_migrate_attachments(state)
        assert state["files"][0]["document_tag"] == "view_360"
