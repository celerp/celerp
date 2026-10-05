"""The CLI bundle importer prints a refused row as a sentence, never a Python dict repr."""
from __future__ import annotations

import asyncio

from celerp.importers.importer import BundleImporter, ImportResult


def test_cli_importer_prints_a_refused_row_as_text(monkeypatch):
    imp = BundleImporter(api_base="http://127.0.0.1:1", token="t", dry_run=False, batch_size=10, verbose=False)
    refusal = {"message": "Item has no sell-by unit", "message_key": "import.item_no_sell_by"}

    async def fake_post(client, endpoint, records):
        return {"created": 0, "skipped": 0, "errors": [refusal]}

    monkeypatch.setattr(imp, "_post_batch", fake_post)
    result = ImportResult()
    asyncio.run(imp._import_entity_type(None, "Items", "item", [{"entity_id": "item:1"}], result))
    out = result.summary()
    assert "Item has no sell-by unit" in out, out
    assert "message_key" not in out and "{'message'" not in out, out
