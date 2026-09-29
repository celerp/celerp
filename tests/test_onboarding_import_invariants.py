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
import uuid
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


# ---------------------------------------------------------------------------
# INV-SETUP-01..04 - setup delegates to the canonical business-type operation.
# Placeholder selection and empty/unknown/blank handling are owned by
# tests/test_setup_business_type.py::TestSetupRender / TestSetupSubmit.
# ---------------------------------------------------------------------------


def _owner_cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


async def _post_setup(vertical: str, *, set_type, patch_company, restart=None):
    from ui.app import app as ui_app
    with patch("ui.api_client.patch_company", new=patch_company), \
         patch("ui.api_client.set_business_type", new=set_type), \
         patch("ui.api_client.restart_system", new=restart or AsyncMock(return_value={})):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            return await c.post(
                "/setup/company",
                data={"vertical": vertical, "currency": "USD", "timezone": "UTC"},
                cookies=_owner_cookies(),
            )


def _pending_patches(patch_company: AsyncMock) -> list:
    return [c for c in patch_company.await_args_list if c.args[1].get("onboarding_pending") is True]


class TestSetupInvariant:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart_required,dest", [(True, "/setup/activating"), (False, "/onboarding")])
    async def test_setup_marks_onboarding_pending_only_after_business_type_applied(self, restart_required, dest):
        order: list[str] = []
        set_type = AsyncMock(side_effect=lambda *a: order.append("set_type") or {"restart_required": restart_required})
        patch_company = AsyncMock(side_effect=lambda tok, data: order.append(
            "pending" if data.get("onboarding_pending") else "details") or {})
        r = await _post_setup("gemstones", set_type=set_type, patch_company=patch_company)
        assert r.headers["location"].endswith(dest)
        set_type.assert_awaited_once()
        assert order == ["details", "set_type", "pending"]

    @pytest.mark.asyncio
    async def test_setup_failure_does_not_mark_onboarding_pending_or_redirect_as_success(self):
        from ui.api_client import APIError
        set_type = AsyncMock(side_effect=APIError(422, "no such preset"))
        patch_company = AsyncMock(return_value={})
        r = await _post_setup("gemstones", set_type=set_type, patch_company=patch_company)
        assert r.status_code == 200 and "location" not in r.headers
        assert _pending_patches(patch_company) == []

    @pytest.mark.asyncio
    async def test_activation_page_lands_on_onboarding(self):
        from ui.app import app as ui_app
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            r = await c.get("/setup/activating", cookies=_owner_cookies())
        assert "window.location.href = '/onboarding'" in r.text
        assert "/dashboard" not in r.text

    def test_setup_has_no_preset_semantics_of_its_own(self):
        """Categories, units and preset settings are written only by the business-type operation."""
        src = (_REPO / "ui" / "routes" / "setup.py").read_text(encoding="utf-8")
        called = {
            node.func.attr for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        forbidden = {n for n in called if "category" in n or "schema" in n or n in {"apply_vertical_preset", "patch_units"}}
        assert not forbidden, forbidden

    def test_activating_status_does_not_call_mutating_api(self):
        tree = ast.parse((_REPO / "ui" / "routes" / "setup.py").read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "activating_status")
        calls = {
            node.func.attr for node in ast.walk(fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not calls & {"post", "patch", "put", "delete"}
        assert not any(n.startswith(("set_", "patch_", "apply_", "restart")) for n in calls)


async def _register(client) -> dict:
    import uuid
    r = await client.post("/auth/register", json={
        "company_name": "SetupCo", "email": f"setup-{uuid.uuid4().hex[:8]}@test.example",
        "name": "Owner", "password": "pwvalid1",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _apply(client, h, vertical: str) -> dict:
    r = await client.post("/companies/me/business-type", json={"vertical": vertical}, headers=h)
    assert r.status_code == 200, r.text
    return (await client.get("/companies/me", headers=h)).json()["settings"]


class TestSetupSentinels:
    """The dimensions a setup-local shortcut used to miss, through the one writer setup calls."""

    @pytest.mark.asyncio
    async def test_setup_gemstones_uses_canonical_category_slugs_display_names_and_units(self, client):
        from celerp.services.units import DEFAULT_UNITS
        from celerp.services.vertical_presets import _UNIT_FIELDS, load_category, load_preset
        h = await _register(client)
        settings = await _apply(client, h, "gemstones")
        units = {u["name"] for u in settings.get("units") or DEFAULT_UNITS}
        for slug in load_preset("gemstones")["categories"]:
            cat = load_category(slug)
            assert slug in settings["category_schemas"]
            assert settings["category_display_names"][slug] == cat.get("display_name", slug)
            for field in _UNIT_FIELDS:
                if cat.get(field):
                    assert cat[field] in units, (slug, field, cat[field])

    @pytest.mark.asyncio
    async def test_setup_food_beverage_applies_fefo_via_canonical_preset(self, client):
        h = await _register(client)
        assert (await _apply(client, h, "food_beverage"))["inventory_method"] == "fefo"

    @pytest.mark.asyncio
    async def test_setup_exact_retry_has_same_company_state(self, client):
        h = await _register(client)
        first = await _apply(client, h, "gemstones")
        second = await _apply(client, h, "gemstones")
        for key in ("category_schemas", "category_display_names", "units", "inventory_method"):
            assert first.get(key) == second.get(key)


# ---------------------------------------------------------------------------
# Mapping: every importer states its required targets and the check enforces them
# ---------------------------------------------------------------------------

def _all_specs() -> dict:
    """Every browser importer spec, keyed by a readable name."""
    from ui.routes import accounting_import, docs_import, inventory, lists_import, settings_import, subscriptions_import
    return {
        "items": inventory._IMPORT_SPEC,
        "lists": lists_import._LIST_IMPORT_SPEC,
        "chart": accounting_import._CHART_SPEC,
        "docs": docs_import._DOC_IMPORT_SPEC,
        "subscriptions": subscriptions_import._SUB_IMPORT_SPEC,
        "locations": settings_import._LOCATION_SPEC,
        "taxes": settings_import._TAX_SPEC,
        "terms": settings_import._TERMS_SPEC,
    }


def _mapping_form(targets: dict[str, str]) -> dict:
    return {f"map__{col}": target for col, target in targets.items()}


class TestMappingInvariant:
    """INV-MAP: required targets are enforced by the one shared mapping check."""

    @pytest.mark.parametrize("name", sorted(_all_specs()))
    def test_required_targets_matrix(self, name):
        spec = _all_specs()[name]
        core = set(spec.cols)
        cols = [f"c_{c}" for c in spec.cols]
        full = {f"c_{c}": c for c in spec.cols}

        # Every target mapped exactly once: clean.
        assert ci.validate_column_mapping(_mapping_form(full), cols, core_fields=core, required_targets=spec.required) == []

        for req in sorted(spec.required):
            # Missing a required target names it.
            missing = {k: v for k, v in full.items() if v != req}
            errs = ci.validate_column_mapping(_mapping_form(missing), cols, core_fields=core, required_targets=spec.required)
            assert errs, (name, req)

            # Mapped twice is a duplicate, not a pass.
            dup = dict(full, extra=req)
            errs = ci.validate_column_mapping(_mapping_form(dup), cols + ["extra"], core_fields=core, required_targets=spec.required)
            assert errs, (name, req)

    def test_custom_field_named_like_a_core_field_is_rejected(self):
        spec = _all_specs()["items"]
        form = {"map__a": "name", "map__b": ci.MAPPING_ATTRIBUTE, "attr_name__b": "Name"}
        errs = ci.validate_column_mapping(form, ["a", "b"], core_fields=set(spec.cols), required_targets=spec.required)
        assert errs

    def test_item_spec_requires_only_name(self):
        from celerp_inventory.services import build_item_import_spec
        from ui.routes.inventory import _IMPORT_SPEC
        assert _IMPORT_SPEC.required == {"name"}
        assert build_item_import_spec([]).required == {"name"}

    def test_every_ui_caller_states_required_targets(self):
        root = Path(__file__).resolve().parents[1] / "ui" / "routes"
        seen = 0
        for path in sorted(root.glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if fname in ("validate_column_mapping", "_csv_validate_column_mapping"):
                    seen += 1
                    kws = {k.arg for k in node.keywords}
                    assert {"core_fields", "required_targets"} <= kws, f"{path.name}:{node.lineno}"
        assert seen >= 9


async def _seed_company(session) -> uuid.UUID:
    from celerp.models.company import Company, Location
    company_id = uuid.uuid4()
    session.add(Company(id=company_id, name="MapCo", slug=f"mapco-{company_id.hex[:8]}", settings={}))
    await session.flush()
    session.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main", type="warehouse", is_default=True))
    await session.commit()
    return company_id


class TestItemSellByResolution:
    """INV-MAP-03: sell_by is resolved per row, not demanded as a mapped column."""

    @pytest.mark.asyncio
    async def test_no_sell_by_with_category_default_is_accepted(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Stone", "category": "diamond"}], upsert=False, dry_run=True)
        assert build.errors == []
        assert build.records[0]["data"]["sell_by"] == "gram"

    @pytest.mark.asyncio
    async def test_no_sell_by_and_no_default_is_rejected_on_the_row(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Widget", "pieces": "1"}], upsert=False, dry_run=True)
        assert build.records == []
        assert [(e["row"], e["field"], e["code"]) for e in build.errors] == [(1, "sell_by", "sell_by_unresolved")]

    @pytest.mark.asyncio
    async def test_unknown_sell_by_is_rejected_on_the_row(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Widget", "sell_by": "furlong", "pieces": "1"}], upsert=False, dry_run=True)
        assert [(e["field"], e["code"]) for e in build.errors] == [("sell_by", "sell_by_invalid")]

    @pytest.mark.asyncio
    async def test_explicit_valid_sell_by_succeeds(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Widget", "sell_by": "Piece", "pieces": "1"}], upsert=False, dry_run=True)
        assert build.errors == []
        assert build.records[0]["data"]["sell_by"] == "piece"
