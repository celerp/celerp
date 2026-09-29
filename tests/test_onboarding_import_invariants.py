# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Fast invariant suite for onboarding and import.

Each test class owns one family of invariants; each test asserts a single
property that must hold for every import path, not one screen's behavior.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui.routes import csv_import as ci

_REPO = Path(__file__).resolve().parent.parent
_COMPANY_A = "company-a"
_COMPANY_B = "company-b"


@pytest.fixture
def stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path / "import_staging"


# ---------------------------------------------------------------------------
# INV-STAGE-01/02/03 - staged import references
# ---------------------------------------------------------------------------


class TestImportStageInvariant:
    def test_import_ref_accepts_only_canonical_token(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "sku\nA\n")
        assert ci._IMPORT_REF_RE.fullmatch(ref)
        assert ci._stage_paths(ref) is not None
        assert ci._stage_paths(ref.upper()) is None

    @pytest.mark.parametrize("bad", [
        "../etc/passwd", "imp_../../x", "..", "imp_" + "0" * 30 + "/..",
    ])
    def test_import_ref_rejects_path_traversal_before_filesystem_access(self, bad, monkeypatch):
        def _boom():
            raise AssertionError("filesystem resolver reached for an invalid ref")
        monkeypatch.setattr(ci, "_stage_dir", _boom)
        assert ci._stage_paths(bad) is None
        assert ci._read_stage(_COMPANY_A, bad) is None
        ci.delete_import_ref(bad)

    @pytest.mark.parametrize("bad", [
        "/tmp/imp_" + "0" * 32, "imp_" + "0" * 31 + "\\", "C:\\imp_" + "0" * 32,
    ])
    def test_import_ref_rejects_absolute_and_backslash_paths(self, bad):
        assert ci._stage_paths(bad) is None

    @pytest.mark.parametrize("bad", [
        "", "0" * 32, "imp_" + "0" * 31, "imp_" + "0" * 33, "xmp_" + "0" * 32,
        "imp_" + "0" * 32 + "\n", " imp_" + "0" * 32, "imp_" + "g" * 32, "imp_" + "0" * 4096,
    ])
    def test_import_ref_rejects_prefix_suffix_and_oversize_tokens(self, bad):
        assert ci._stage_paths(bad) is None

    def test_import_refs_do_not_collide(self, stage_dir):
        refs = {ci._write_stage(_COMPANY_A, "x") for _ in range(500)}
        assert len(refs) == 500

    def test_import_stage_same_company_loads(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "sku\nA\n")
        assert ci._read_stage(_COMPANY_A, ref) == "sku\nA\n"

    def test_import_stage_wrong_company_fails_closed(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "sku\nA\n")
        assert ci._read_stage(_COMPANY_B, ref) is None
        assert ci._read_stage("", ref) is None

    def test_import_stage_missing_metadata_fails_closed(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "sku\nA\n")
        (stage_dir / f"{ref}.meta").unlink()
        assert ci._read_stage(_COMPANY_A, ref) is None

    def test_import_stage_requires_company(self, stage_dir):
        with pytest.raises(ValueError):
            ci._write_stage("", "sku\nA\n")

    @pytest.mark.asyncio
    async def test_authenticated_load_uses_the_callers_company(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "sku\nA\n")
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_B})):
            assert await ci.load_import_csv("tok", ref) is None
            assert await ci.resolve_import_csv("tok", {"csv_ref": ref}) == ""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A})):
            assert await ci.resolve_import_csv("tok", {"csv_ref": ref}) == "sku\nA\n"

    def test_expired_stage_is_rejected_and_removed(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "sku\nA\n")
        meta = stage_dir / f"{ref}.meta"
        stale = time.time() - ci._IMPORT_STAGE_MAX_AGE_SECONDS - 1
        meta.write_text(json.dumps({"company_id": _COMPANY_A, "created_at": stale}))
        assert ci._read_stage(_COMPANY_A, ref) is None
        assert not meta.exists() and not (stage_dir / f"{ref}.csv").exists()

    def test_cleanup_never_removes_recent_stage(self, stage_dir):
        fresh = ci._write_stage(_COMPANY_A, "fresh")
        old = ci._write_stage(_COMPANY_A, "old")
        stale = time.time() - ci._IMPORT_STAGE_MAX_AGE_SECONDS - 1
        (stage_dir / f"{old}.meta").write_text(json.dumps({"company_id": _COMPANY_A, "created_at": stale}))
        assert ci.cleanup_expired_import_refs() == 1
        assert ci._read_stage(_COMPANY_A, fresh) == "fresh"

    def test_no_route_constructs_a_staging_path(self):
        """Only the staging helper may name the stage directory or its private resolvers."""
        private = {"_stage_dir", "_stage_paths", "_write_stage", "_read_stage"}
        offenders = []
        for path in (_REPO / "ui").rglob("*.py"):
            if path.name == "csv_import.py":
                continue
            src = path.read_text(encoding="utf-8")
            if "import_staging" in src:
                offenders.append(f"{path}: import_staging")
            for node in ast.walk(ast.parse(src)):
                name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
                if name in private:
                    offenders.append(f"{path}:{node.lineno}: {name}")
        assert not offenders, offenders

    async def _confirm(self, stage_dir, import_rows):
        from ui.app import app as ui_app
        ref = ci._write_stage(_COMPANY_A, "sku,name,sell_by\nA-1,Ruby,piece\n")
        company = {"id": _COMPANY_A, "current_role": "owner", "settings": {}}
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
             patch("ui.api_client.import_rows", new=import_rows):
            async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                r = await c.post(
                    "/inventory/import/confirm",
                    data={"csv_ref": ref},
                    cookies={"celerp_token": make_test_token(role="owner")},
                )
        assert r.status_code == 200
        return ref

    @pytest.mark.asyncio
    async def test_successful_terminal_commit_can_remove_stage(self, stage_dir):
        ok = AsyncMock(return_value={"created": 1, "skipped": 0, "updated": 0, "errors": []})
        ref = await self._confirm(stage_dir, ok)
        assert ok.await_count == 1
        assert ci._read_stage(_COMPANY_A, ref) is None

    @pytest.mark.asyncio
    async def test_failed_commit_keeps_stage_for_retry(self, stage_dir):
        from ui.api_client import APIError
        failing = AsyncMock(side_effect=APIError(500, "boom"))
        ref = await self._confirm(stage_dir, failing)
        assert ci._read_stage(_COMPANY_A, ref) is not None
