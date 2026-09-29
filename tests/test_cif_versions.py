# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Two CIF compatibility surfaces, versioned separately: the standalone CIF v1
formats (JSONL records and the bundle manifest read by the bundle importer) keep
importing exactly as before, and the migration manifest carries its own version
with its own guard."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from migration_support import load_run, migration_env, real_engine, staged_run  # noqa: F401 - fixtures

FIXTURE = Path(__file__).parent / "fixtures" / "cif_v1"


async def _import(manifest) -> tuple[list[dict], object]:
    from celerp.importers.importer import BundleImporter

    posted: list[dict] = []

    async def post(client, endpoint, records):
        posted.append({"endpoint": endpoint, "records": records})
        return {"created": len(records), "skipped": 0, "errors": []}

    importer = BundleImporter(api_base="http://localhost", token="tok")
    with patch.object(importer, "_post_batch", new=AsyncMock(side_effect=post)):
        result = await importer.run(manifest)
    return posted, result


@pytest.mark.asyncio
async def test_a_v1_manifest_generated_on_main_imports_unchanged():
    """manifest.json was written by the bundle schema as released; posted.json is
    what the released bundle importer sent for it."""
    from celerp.importers.importer import load_manifest

    expected = json.loads((FIXTURE / "posted.json").read_text())
    manifest = load_manifest(FIXTURE / "manifest.json")

    posted, result = await _import(manifest)

    assert json.loads(json.dumps(posted)) == expected["posted"]
    assert (result.created, result.failed) == (expected["created"], expected["failed"])


def test_a_v1_manifest_dry_run_reports_its_stats(capsys):
    import asyncio

    from celerp.importers.importer import BundleImporter, load_manifest

    manifest = load_manifest(FIXTURE / "manifest.json")
    asyncio.run(BundleImporter(api_base="http://localhost", token="tok", dry_run=True).run(manifest))

    out = capsys.readouterr().out
    assert "Stats from manifest:" in out
    assert "items: 2" in out and "documents: 1" in out


def test_v1_low_level_records_and_batches_still_validate():
    from celerp.importers.schema import CIF_VERSION, CIFBatch, CIFRecord

    assert CIF_VERSION == "1"
    assert CIFBatch(source="s", source_system="x").cif_version == "1"
    assert CIFBatch.model_validate({"cif_version": "1", "source": "s", "source_system": "x"}).cif_version == "1"
    record = CIFRecord.model_validate({
        "cif_version": "1", "entity_id": "item:1", "entity_type": "item", "event_type": "item.snapshot",
        "data": {"name": "Ruby"}, "source": "import:old", "idempotency_key": "k:1"})
    assert record.cif_version == "1"


def test_the_migration_manifest_has_its_own_version_and_guard():
    from celerp.importers.schema import CIF_VERSION, MIGRATION_CIF_VERSION, CIFImportManifest

    assert MIGRATION_CIF_VERSION == "2" != CIF_VERSION
    assert CIFImportManifest.model_fields["cif_version"].default == MIGRATION_CIF_VERSION
    base = {"source": "s", "source_system": "x", "adapter_version": "1", "exported_at": "2026-01-01T00:00:00",
            "bundle": {}}
    assert CIFImportManifest.model_validate({**base, "cif_version": "2"}).cif_version == "2"
    for stale in ("1", "3"):
        with pytest.raises(ValidationError, match=f"Unsupported migration CIF version '{stale}'"):
            CIFImportManifest.model_validate({**base, "cif_version": stale})


@pytest.mark.asyncio
async def test_a_migration_run_records_the_migration_cif_version(real_engine, migration_env):
    from celerp.importers.schema import MIGRATION_CIF_VERSION

    run_id, _, _ = await staged_run(real_engine)

    assert (await load_run(real_engine, run_id)).cif_version == MIGRATION_CIF_VERSION
