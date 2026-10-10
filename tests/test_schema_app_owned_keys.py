"""A field the user defines with a key only the app writes could never be filled (every item
write refuses it), so the item and category schemas refuse it when it is defined, and Settings
derives a key that is not app-owned from a label such as "Files"."""
from __future__ import annotations

import pytest

from celerp.services.field_schema import SYSTEM_ITEM_KEYS


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/companies/me/category-schema/Rings", "/companies/me/item-schema"])
async def test_a_schema_refuses_a_field_keyed_like_an_app_owned_item_field(client, auth, path):
    before = (await client.get(path, headers=auth["headers"])).json()
    fields = [f for f in (before if isinstance(before, list) else []) if isinstance(f, dict)]
    r = await client.patch(path, headers=auth["headers"], json={"fields": [
        *fields, {"key": "files", "label": "Files", "type": "text"}]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["message_key"] == "item.app_owned_fields", r.text
    assert (await client.get(path, headers=auth["headers"])).json() == before


def test_settings_derives_a_key_that_is_not_app_owned():
    from ui.routes.settings import _derive_key
    for label in ("Files", "Attachments", "Children", "Reserved quantity"):
        assert _derive_key(label, set()) not in SYSTEM_ITEM_KEYS, label
    assert _derive_key("Files", set()) == "files_2"
