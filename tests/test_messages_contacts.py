# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Contact messages say what went wrong and what to do, without internal ids."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from ui.i18n import t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


async def _headers(client) -> dict:
    r = await client.post("/auth/register", json={
        "company_name": "CtcMsgCo", "email": f"ctc-{uuid.uuid4().hex[:8]}@test.test",
        "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.mark.asyncio
async def test_missing_contact_says_refresh_and_choose_again(client):
    r = await client.get("/crm/contacts/contact:nope", headers=await _headers(client))
    assert r.status_code == 404
    assert r.json()["detail"] == t("contacts.err_contact_not_found", "en")


@pytest.mark.asyncio
async def test_bulk_delete_of_missing_contact_hides_the_id(client):
    r = await client.post("/crm/contacts/bulk/delete", headers=await _headers(client),
                          json={"contact_ids": ["contact:secret-id"]})
    assert r.status_code == 404
    assert "secret-id" not in r.json()["detail"]
    assert r.json()["detail"] == t("contacts.err_selected_not_found", "en")


@pytest.mark.asyncio
async def test_merge_with_no_sources_says_choose_one(client):
    r = await client.post("/crm/contacts/merge", headers=await _headers(client),
                          json={"source_contact_ids": [], "target_contact_id": "contact:x"})
    assert r.status_code == 422
    assert r.json()["detail"] == t("contacts.err_merge_no_sources", "en")


def test_prefix_keys_are_gone():
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))
    for key in ("contacts.delete_failed_prefix", "contacts.merge_failed_prefix",
                "contacts.error_prefix", "contacts.delete_failed_dot"):
        assert key not in en


@pytest.mark.parametrize("key, says", [
    ("error.record_not_found", "Go back, refresh the page"),
    ("contacts.err_resubmitted", "possibly from another tab"),
    ("contacts.err_file_not_found", "It may have been deleted."),
    ("contacts.err_name_required", "Enter a name for the contact"),
    ("contacts.err_file_missing", "restore it from a backup"),
    ("contacts.err_none_selected", "Tick the contacts you want"),
    ("contacts.err_merge_into_itself", "can't be merged into itself"),
    ("contacts.err_merge_target_not_found", "the contact you chose to keep"),
    ("contacts.err_merge_target_deleted", "Choose a different contact to merge into."),
    ("contacts.err_merge_source_not_found", "one of the contacts to merge"),
    ("contacts.err_merge_source_deleted", "Remove it from the selection."),
    ("contacts.err_merge_source_merged", "already merged into another contact"),
    ("contacts.delete_failed", "Refresh the page and try again."),
    ("contacts.merge_failed", "Refresh the page and try again."),
    ("contacts.csv_expired", "Upload it again."),
    ("contacts.invalid_currency", "Choose a currency from the list."),
])
def test_contact_messages_say_what_to_do(key, says):
    assert says in t(key, "en", code="XYZ")
