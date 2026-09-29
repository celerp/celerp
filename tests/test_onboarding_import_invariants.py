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
                    data={"csv_ref": ref, "preview_hash": "c" * 64},
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


# ---------------------------------------------------------------------------
# Preview and commit: one semantic preview, and a commit bound to it
# ---------------------------------------------------------------------------

def _codes(errors: list[dict]) -> list[tuple]:
    return sorted((e["row"], e["field"], e.get("code")) for e in errors)


async def _item_count(session, company_id: str) -> int:
    from sqlalchemy import func, select

    from celerp.models.projections import Projection
    return (await session.execute(
        select(func.count()).select_from(Projection).where(
            Projection.company_id == uuid.UUID(company_id), Projection.entity_type == "item",
        )
    )).scalar_one()


async def _rows_preview(client, h, rows, *, upsert=False, key="op-1") -> dict:
    r = await client.post("/items/import/rows/preview", json={"rows": rows, "upsert": upsert, "idempotency_key": key}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _rows_commit(client, h, rows, *, upsert=False, key="op-1", preview_hash=None):
    return await client.post("/items/import/rows", json={
        "rows": rows, "upsert": upsert, "idempotency_key": key, "preview_hash": preview_hash,
    }, headers=h)


async def _seed_items(client, h, rows, key):
    r = await _rows_commit(client, h, rows, key=key)
    assert r.status_code == 200 and not r.json()["errors"], r.text


async def _set_company_settings(session, company_id, **values) -> None:
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, uuid.UUID(str(company_id)))
    company.settings = {**(company.settings or {}), **values}
    await session.commit()


@pytest.fixture
async def perm(client, session):
    from celerp.services.auth import decode_access_token
    from test_helpers import perm_setup
    s = await perm_setup(client, session)
    claims = decode_access_token(s["admin_h"]["Authorization"].split()[1])
    s["company_id"], s["admin_user_id"] = claims["company_id"], claims["sub"]
    # Two categories share a display label so an ambiguous label can be imported.
    await _set_company_settings(
        session, s["company_id"],
        category_schemas={"red_a": [], "red_b": []},
        category_display_names={"red_a": "Red", "red_b": "Red"},
    )
    return s


@pytest.fixture
def write_upload():
    """Seed an owned transient upload for the admin, as the upload endpoint would."""
    from celerp.ai.files import upload_dir

    def _write(perm: dict, text: str, *, filename: str = "items.csv") -> str:
        file_id = f"ai_up_{uuid.uuid4().hex}"
        data = text.encode()
        (upload_dir() / f"{file_id}.bin").write_bytes(data)
        (upload_dir() / f"{file_id}.meta").write_text(json.dumps({
            "filename": filename, "content_type": "text/csv", "size": len(data),
            "company_id": perm["company_id"], "user_id": perm["admin_user_id"],
        }))
        return file_id
    return _write


# (case id, seed rows, rows, upsert, role header key, expected (row, field, code))
_PARITY_CASES = [
    ("missing_name", None, [{"sell_by": "piece", "quantity": "1"}], False, "admin_h", [(1, "name", "required")]),
    ("resolved_sell_by", None, [{"name": "Stone", "category": "diamond", "weight": "1.5", "weight_unit": "gram"}], False, "admin_h", []),
    ("weight_unit_mismatch", None, [{"name": "Stone", "category": "diamond", "weight": "1.5", "weight_unit": "carat"}], False, "admin_h", [(1, "weight", "weight_unit_mismatch")]),
    ("category_ambiguous", None, [{"name": "Stone", "category": "Red", "sell_by": "piece"}], False, "admin_h", [(1, "category", "category_ambiguous")]),
    ("price_basis_mismatch", None, [{"name": "Stone", "sell_by": "gram", "weight": "2", "weight_unit": "gram", "retail_price": "10", "retail_price_basis": "carat"}], False, "admin_h", [(1, "retail_price", "price_basis_mismatch")]),
    ("unresolved_sell_by", None, [{"name": "Widget", "quantity": "1"}], False, "admin_h", [(1, "sell_by", "sell_by_unresolved")]),
    ("invalid_unit", None, [{"name": "Widget", "sell_by": "furlong", "quantity": "1"}], False, "admin_h", [(1, "sell_by", "sell_by_invalid")]),
    ("default_location", None, [{"name": "Widget", "sell_by": "piece", "quantity": "1"}], False, "admin_h", []),
    ("location_create_allowed", None, [{"name": "Widget", "sell_by": "piece", "quantity": "1", "location_name": "Annex"}], False, "admin_h", []),
    ("location_create_denied", None, [{"name": "Widget", "sell_by": "piece", "quantity": "1", "location_name": "Annex"}], False, "manager_h", [(1, "location_name", "location_create_denied")]),
    ("unique_sku_upsert", [{"name": "One", "sku": "U-1", "sell_by": "piece", "quantity": "1"}],
     [{"name": "One renamed", "sku": "U-1", "sell_by": "piece"}], True, "admin_h", []),
    ("ambiguous_sku_upsert", [{"name": "A", "sku": "AMB", "sell_by": "piece", "quantity": "1"}, {"name": "B", "sku": "AMB", "sell_by": "piece", "quantity": "1"}],
     [{"name": "Which", "sku": "AMB", "sell_by": "piece"}], True, "admin_h", [(1, "sku", "sku_ambiguous")]),
    ("shared_barcode", [{"name": "A", "sku": "SB-A", "barcode": "7508", "sell_by": "piece", "quantity": "1"}, {"name": "B", "sku": "SB-B", "barcode": "7508", "sell_by": "piece", "quantity": "1"}],
     [{"name": "Which", "barcode": "7508", "sell_by": "piece"}], True, "admin_h", [(1, "barcode", "barcode_ambiguous")]),
    ("sku_barcode_conflict", [{"name": "A", "sku": "CX-A", "barcode": "1111", "sell_by": "piece", "quantity": "1"}, {"name": "B", "sku": "CX-B", "barcode": "2222", "sell_by": "piece", "quantity": "1"}],
     [{"name": "Mixed", "sku": "CX-A", "barcode": "2222", "sell_by": "piece"}], True, "admin_h", [(1, "sku", "sku_barcode_conflict")]),
    ("sell_by_change_without_quantity", [{"name": "A", "sku": "SC-1", "sell_by": "piece", "quantity": "1"}],
     [{"name": "A", "sku": "SC-1", "sell_by": "gram"}], True, "admin_h", [(1, "sell_by", "sell_by_change_needs_quantity")]),
    ("price_total_derivation", None, [{"name": "Widget", "sell_by": "piece", "pieces": "4", "retail_price_total": "100"}], False, "admin_h", []),
]


class TestPreviewCommitInvariant:
    """INV-PREVIEW-01..04: one preview helper, a commit that recomputes it, a hash
    that binds every input, and no row on which preview and commit disagree."""

    # INV-PREVIEW-04 --------------------------------------------------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", _PARITY_CASES, ids=[c[0] for c in _PARITY_CASES])
    async def test_preview_and_bound_commit_agree(self, client, session, perm, case):
        _id, seed, rows, upsert, who, expected = case
        h = perm[who]
        if seed:
            await _seed_items(client, perm["admin_h"], seed, key=f"seed-{_id}")
        preview = await _rows_preview(client, h, rows, upsert=upsert, key=f"op-{_id}")
        assert _codes(preview["errors"]) == sorted(expected)

        before = await _item_count(session, perm["company_id"])
        r = await _rows_commit(client, h, rows, upsert=upsert, key=f"op-{_id}", preview_hash=preview["preview_hash"])
        if expected:
            assert r.status_code == 422, r.text
            assert _codes(r.json()["detail"]["errors"]) == sorted(expected)
            assert await _item_count(session, perm["company_id"]) == before
        else:
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["errors"] == []
            assert body["created"] + body["updated"] == len(rows)

    @pytest.mark.asyncio
    async def test_allowed_location_is_announced_by_preview(self, client, perm):
        rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1", "location_name": "Annex"}]
        assert (await _rows_preview(client, perm["admin_h"], rows))["locations_to_create"] == ["Annex"]

    # INV-PREVIEW-02 --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_commit_recomputes_semantic_preview_before_writer(self, client, perm, monkeypatch):
        import celerp_inventory.routes as routes
        order: list[str] = []
        real_preview, real_writer = routes.preview_import_rows, routes.import_items

        async def preview_spy(*a, **k):
            order.append("preview")
            return await real_preview(*a, **k)

        async def writer_spy(*a, **k):
            order.append("writer")
            return await real_writer(*a, **k)

        rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1"}]
        ph = (await _rows_preview(client, perm["admin_h"], rows))["preview_hash"]
        monkeypatch.setattr(routes, "preview_import_rows", preview_spy)
        monkeypatch.setattr(routes, "import_items", writer_spy)
        r = await _rows_commit(client, perm["admin_h"], rows, preview_hash=ph)
        assert r.status_code == 200, r.text
        assert order == ["preview", "writer"]

    @pytest.mark.asyncio
    async def test_commit_does_not_call_writer_when_preview_has_errors(self, client, perm, monkeypatch):
        import celerp_inventory.routes as routes
        rows = [{"name": "Widget", "quantity": "1"}]
        ph = (await _rows_preview(client, perm["admin_h"], rows))["preview_hash"]
        writer = AsyncMock()
        monkeypatch.setattr(routes, "import_items", writer)
        r = await _rows_commit(client, perm["admin_h"], rows, preview_hash=ph)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "validation_failed"
        writer.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("change", ["row_edited", "upsert", "operation_key"])
    async def test_commit_does_not_call_writer_when_preview_hash_is_stale(self, client, perm, monkeypatch, change):
        import celerp_inventory.routes as routes
        rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1"}]
        ph = (await _rows_preview(client, perm["admin_h"], rows))["preview_hash"]
        writer = AsyncMock()
        monkeypatch.setattr(routes, "import_items", writer)
        kwargs = {"upsert": False, "key": "op-1"}
        if change == "row_edited":
            rows = [dict(rows[0], quantity="2")]
        elif change == "upsert":
            kwargs["upsert"] = True
        else:
            kwargs["key"] = "op-2"
        r = await _rows_commit(client, perm["admin_h"], rows, preview_hash=ph, **kwargs)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "preview_stale"
        writer.assert_not_awaited()

    # INV-PREVIEW-01 --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_browser_row_preview_delegates_to_same_semantic_row_preview(self, client, perm, monkeypatch):
        import celerp_inventory.routes as routes
        spy = AsyncMock(wraps=routes.preview_import_rows)
        monkeypatch.setattr(routes, "preview_import_rows", spy)
        await _rows_preview(client, perm["admin_h"], [{"name": "Widget", "sell_by": "piece"}])
        assert spy.await_count == 1

    @pytest.mark.asyncio
    async def test_file_preview_delegates_to_semantic_row_preview(self, client, perm, monkeypatch, write_upload):
        import celerp_inventory.routes as routes
        spy = AsyncMock(wraps=routes.preview_import_rows)
        monkeypatch.setattr(routes, "preview_import_rows", spy)
        fid = write_upload(perm, "sku,name,sell_by\nF-1,Widget,piece\n")
        r = await client.get(f"/items/import/preview?file_id={fid}", headers=perm["admin_h"])
        assert r.status_code == 200, r.text
        assert spy.await_count == 1
        assert spy.await_args.args[4] == [{"sku": "F-1", "name": "Widget", "sell_by": "piece"}]

    @pytest.mark.asyncio
    async def test_file_and_row_preview_report_the_same_semantic_errors(self, client, perm, write_upload):
        fid = write_upload(perm, "name,sell_by,quantity\nGood,piece,1\nNo unit,,1\nBad unit,furlong,1\n")
        file_errors = (await client.get(f"/items/import/preview?file_id={fid}", headers=perm["admin_h"])).json()["errors"]
        rows = [
            {"name": "Good", "sell_by": "piece", "quantity": "1"},
            {"name": "No unit", "sell_by": "", "quantity": "1"},
            {"name": "Bad unit", "sell_by": "furlong", "quantity": "1"},
        ]
        row_errors = (await _rows_preview(client, perm["admin_h"], rows))["errors"]
        assert _codes(file_errors) == _codes(row_errors) == [(2, "sell_by", "sell_by_unresolved"), (3, "sell_by", "sell_by_invalid")]

    # INV-PREVIEW-03 --------------------------------------------------------

    def test_preview_hash_changes_when_row_changes(self):
        from celerp_inventory.routes import _rows_preview_hash
        assert _rows_preview_hash([{"name": "A"}], False, "k") != _rows_preview_hash([{"name": "B"}], False, "k")

    def test_preview_hash_changes_when_upsert_changes(self):
        from celerp_inventory.routes import _rows_preview_hash
        assert _rows_preview_hash([{"name": "A"}], False, "k") != _rows_preview_hash([{"name": "A"}], True, "k")

    def test_preview_hash_changes_when_operation_key_changes(self):
        from celerp_inventory.routes import _rows_preview_hash
        assert _rows_preview_hash([{"name": "A"}], False, "k1") != _rows_preview_hash([{"name": "A"}], False, "k2")

    def test_preview_hash_is_stable_for_equivalent_dict_key_order(self):
        from celerp_inventory.routes import _rows_preview_hash
        assert _rows_preview_hash([{"name": "A", "sku": "1"}], False, "k") == _rows_preview_hash([{"sku": "1", "name": "A"}], False, "k")

    @pytest.mark.asyncio
    async def test_file_preview_hash_changes_when_file_bytes_change(self, client, perm, write_upload):
        a = write_upload(perm, "name,sell_by\nWidget,piece\n")
        b = write_upload(perm, "name,sell_by\nWidget,gram\n")
        ha = (await client.get(f"/items/import/preview?file_id={a}", headers=perm["admin_h"])).json()["preview_hash"]
        hb = (await client.get(f"/items/import/preview?file_id={b}", headers=perm["admin_h"])).json()["preview_hash"]
        assert ha != hb

    @pytest.mark.asyncio
    async def test_file_preview_hash_changes_when_mapping_changes(self, client, perm, write_upload):
        fid = write_upload(perm, "title,unit\nWidget,piece\n")
        hashes = []
        for mapping in ({"title": "name", "unit": "sell_by"}, {"title": "name", "unit": "__skip__"}):
            r = await client.post("/items/import/preview", json={"file_id": fid, "mapping": mapping}, headers=perm["admin_h"])
            assert r.status_code == 200, r.text
            hashes.append(r.json()["preview_hash"])
        assert hashes[0] != hashes[1]

    @pytest.mark.asyncio
    async def test_file_preview_hash_changes_when_sheet_changes(self, client, perm, write_upload, monkeypatch):
        import celerp.importers.tabular as tabular
        monkeypatch.setattr(tabular, "read_table", lambda data, filename, sheet=None: (["name"], [{"name": "Widget"}]))
        fid = write_upload(perm, "not really a workbook", filename="items.xlsx")
        ha = (await client.get(f"/items/import/preview?file_id={fid}&sheet=One", headers=perm["admin_h"])).json()["preview_hash"]
        hb = (await client.get(f"/items/import/preview?file_id={fid}&sheet=Two", headers=perm["admin_h"])).json()["preview_hash"]
        assert ha != hb

    # Whole-import identity -------------------------------------------------

    @pytest.mark.asyncio
    async def test_large_import_is_one_semantic_build_with_bounded_writes(self, client, perm, monkeypatch):
        import celerp_inventory.services as svc
        builds, batches = [], []
        real_build, real_commit = svc.build_import_records, svc.commit_import_batch

        async def build_spy(*a, **k):
            builds.append(len(a[2]))
            return await real_build(*a, **k)

        async def commit_spy(session, company_id, user, role, settings, body):
            batches.append(len(body.records))
            return await real_commit(session, company_id, user, role, settings, body)

        rows = [{"name": f"Item {i}", "sell_by": "piece", "quantity": "1"} for i in range(501)]
        ph = (await _rows_preview(client, perm["admin_h"], rows, key="big"))["preview_hash"]
        monkeypatch.setattr(svc, "build_import_records", build_spy)
        monkeypatch.setattr(svc, "commit_import_batch", commit_spy)
        r = await _rows_commit(client, perm["admin_h"], rows, key="big", preview_hash=ph)
        assert r.status_code == 200, r.text
        assert r.json()["created"] == 501
        assert builds.count(501) == 2  # the bound preview, then the writer: each over the whole import
        assert batches == [500, 1]

    @pytest.mark.asyncio
    async def test_identical_rows_are_distinct_creates_and_exact_retry_is_a_no_op(self, client, session, perm):
        rows = [{"name": "Same", "sell_by": "piece", "quantity": "1"}] * 2
        ph = (await _rows_preview(client, perm["admin_h"], rows, key="twins"))["preview_hash"]
        first = await _rows_commit(client, perm["admin_h"], rows, key="twins", preview_hash=ph)
        assert first.json()["created"] == 2
        count = await _item_count(session, perm["company_id"])
        again = await _rows_commit(client, perm["admin_h"], rows, key="twins", preview_hash=ph)
        assert again.status_code == 200 and again.json()["created"] == 0
        assert await _item_count(session, perm["company_id"]) == count

    @pytest.mark.asyncio
    async def test_rows_envelope_is_bounded_by_the_tabular_limits(self, client, perm):
        from celerp.importers.tabular import MAX_CELLS, MAX_ROWS
        too_many_rows = [{"name": "x"}] * (MAX_ROWS + 1)
        r = await client.post("/items/import/rows/preview", json={"rows": too_many_rows}, headers=perm["admin_h"])
        assert r.status_code == 422
        wide = [{f"c{j}": "v" for j in range(MAX_CELLS // 10 + 1)}] * 10
        r = await client.post("/items/import/rows/preview", json={"rows": wide}, headers=perm["admin_h"])
        assert r.status_code == 422

    # Browser: nothing is importable until the server review is clean -------

    async def _ui_post(self, path, data, *, preview, import_rows=None):
        from ui.app import app as ui_app
        company = {"id": _COMPANY_A, "current_role": "owner", "settings": {}}
        import_rows = import_rows or AsyncMock(return_value={"created": 1, "skipped": 0, "updated": 0, "errors": []})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
             patch("ui.api_client.preview_import_rows", new=preview), \
             patch("ui.api_client.import_rows", new=import_rows):
            async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                r = await c.post(path, data=data, cookies=_owner_cookies())
        assert r.status_code == 200, r.text
        return r.text, import_rows

    @pytest.mark.asyncio
    async def test_review_with_row_errors_offers_no_import(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "name,quantity\nWidget,1\n")
        preview = AsyncMock(return_value={"errors": [{"row": 1, "field": "sell_by", "code": "sell_by_unresolved", "message": "No selling unit"}],
                                          "locations_to_create": [], "preview_hash": "d" * 64})
        html, _ = await self._ui_post("/inventory/import/review", {"csv_ref": ref}, preview=preview)
        assert "No selling unit" in html
        assert "d" * 64 not in html
        assert 'hx-post="/inventory/import/confirm"' not in html

    @pytest.mark.asyncio
    async def test_changing_update_existing_reruns_review(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "name,sku,sell_by\nWidget,W-1,piece\n")
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "e" * 64})
        html, _ = await self._ui_post("/inventory/import/review", {"csv_ref": ref, "upsert": "1"}, preview=preview)
        assert preview.await_args.kwargs["upsert"] is True
        assert 'name="preview_hash" value="' + "e" * 64 in html
        assert 'hx-post="/inventory/import/review"' in html

    @pytest.mark.asyncio
    async def test_confirm_without_reviewed_hash_never_imports(self, stage_dir):
        ref = ci._write_stage(_COMPANY_A, "name,sell_by\nWidget,piece\n")
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "f" * 64})
        html, writer = await self._ui_post("/inventory/import/confirm", {"csv_ref": ref}, preview=preview)
        writer.assert_not_awaited()
        assert "f" * 64 in html

    @pytest.mark.asyncio
    async def test_stale_confirm_returns_to_review(self, stage_dir):
        from ui.api_client import APIError
        ref = ci._write_stage(_COMPANY_A, "name,sell_by\nWidget,piece\n")
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "a" * 64})
        stale = AsyncMock(side_effect=APIError(409, {"code": "preview_stale"}))
        html, _ = await self._ui_post("/inventory/import/confirm", {"csv_ref": ref, "preview_hash": "b" * 64},
                                      preview=preview, import_rows=stale)
        assert "a" * 64 in html
        assert ci._read_stage(_COMPANY_A, ref) is not None


# ---------------------------------------------------------------------------
# Categories, units and prices mean what the source meant
# ---------------------------------------------------------------------------

async def _item_states(session, company_id: str) -> list[dict]:
    from sqlalchemy import select

    from celerp.models.projections import Projection
    session.expire_all()
    return [p.state for p in (await session.execute(
        select(Projection).where(Projection.company_id == uuid.UUID(company_id), Projection.entity_type == "item")
    )).scalars().all()]


async def _import_clean(client, h, rows, key):
    ph = (await _rows_preview(client, h, rows, key=key))["preview_hash"]
    r = await _rows_commit(client, h, rows, key=key, preview_hash=ph)
    assert r.status_code == 200 and r.json()["errors"] == [], r.text


_GEM_CATEGORIES = {"ruby": [], "sapphire": []}
_GEM_NAMES = {"ruby": "Ruby", "sapphire": "Blue Stone"}


class TestCategoryInvariant:
    """INV-CAT-01: known labels resolve to the canonical key; ambiguity fails; unknown stays."""

    @pytest.mark.parametrize("value,expected", [
        ("ruby", "ruby"),
        ("RUBY", "ruby"),
        ("Ruby", "ruby"),
        ("  ruby  ", "ruby"),
        ("blue stone", "sapphire"),
        ("Opal", "Opal"),
        ("", ""),
    ])
    def test_resolver_table(self, value, expected):
        from celerp_inventory.services import resolve_import_category
        assert resolve_import_category(value, _GEM_CATEGORIES, _GEM_NAMES) == (expected, None)

    def test_ambiguous_display_label_fails(self):
        from celerp_inventory.services import resolve_import_category
        category, error = resolve_import_category("red", ["red_a", "red_b"], {"red_a": "Red", "red_b": "Red"})
        assert error and "red_a" in error and "red_b" in error

    def test_ambiguous_casefolded_key_fails(self):
        from celerp_inventory.services import resolve_import_category
        assert resolve_import_category("RUBY", ["Ruby", "ruby"], {})[1]

    def test_exact_key_beats_another_categorys_label(self):
        from celerp_inventory.services import resolve_import_category
        assert resolve_import_category("ruby", ["ruby", "gem"], {"gem": "ruby"}) == ("ruby", None)

    @pytest.mark.asyncio
    async def test_label_and_slug_drive_the_same_default_unit_and_key(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        await _set_company_settings(session, cid, category_schemas=_GEM_CATEGORIES, category_display_names=_GEM_NAMES)
        build = await build_import_records(session, cid, [
            {"name": "A", "category": "Ruby", "quantity": "1"},
            {"name": "B", "category": "ruby", "quantity": "1"},
        ], upsert=False, dry_run=True)
        assert build.errors == []
        assert [(r["data"]["category"], r["data"]["sell_by"]) for r in build.records] == [("ruby", "gram"), ("ruby", "gram")]

    @pytest.mark.asyncio
    async def test_unknown_category_is_kept_as_custom(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        await _set_company_settings(session, cid, category_schemas=_GEM_CATEGORIES, category_display_names=_GEM_NAMES)
        build = await build_import_records(session, cid, [{"name": "A", "category": "Opal", "sell_by": "piece"}], upsert=False, dry_run=True)
        assert build.records[0]["data"]["category"] == "Opal"

    @pytest.mark.asyncio
    async def test_inferred_attributes_attach_to_the_canonical_category(self, client, session, perm):
        from celerp.models.company import Company
        await _set_company_settings(session, perm["company_id"], category_schemas=_GEM_CATEGORIES, category_display_names=_GEM_NAMES)
        await _import_clean(client, perm["admin_h"], [{"name": "A", "category": "Ruby", "sell_by": "piece", "quantity": "1", "hue": "pigeon blood"}], "cat-attr")
        session.expire_all()
        schemas = (await session.get(Company, uuid.UUID(perm["company_id"]))).settings["category_schemas"]
        assert "Ruby" not in schemas
        assert [f["key"] for f in schemas["ruby"]] == ["hue"]


class TestUnitAndPriceInvariant:
    """INV-UNIT-01/02 and INV-PRICE-01: no silent weight or currency conversion."""

    # INV-UNIT-01 -----------------------------------------------------------

    @pytest.mark.parametrize("row,sell_by,expected", [
        ({"quantity": "3", "weight": "1.5", "weight_unit": "carat"}, "gram", (3.0, None)),
        ({"weight": "1.5", "weight_unit": "carat"}, "carat", (1.5, None)),
        ({"weight": "1.5", "weight_unit": "Carat"}, "carat", (1.5, None)),
        ({"weight_ct": "1.5"}, "carat", (1.5, None)),
        ({"weight": "1.5", "weight_unit": "carat"}, "gram", (0.0, "weight_unit_mismatch")),
        ({"weight_ct": "1.5"}, "gram", (0.0, "weight_unit_mismatch")),
        ({"weight": "1.5"}, "gram", (0.0, "weight_unit_unknown")),
        ({"weight": "1.5", "weight_unit": "stone"}, "gram", (0.0, "weight_unit_unknown")),
        ({"pieces": "4", "weight": "1.5"}, "piece", (4.0, None)),
        ({}, "carat", (0.0, None)),
    ])
    def test_quantity_derivation_table(self, row, sell_by, expected):
        from celerp.services.units import DEFAULT_UNITS, build_unit_map
        from celerp_inventory.services import _derive_import_qty
        unit_canonical = {u["name"].lower(): u["name"] for u in DEFAULT_UNITS}
        qty, error = _derive_import_qty(row, sell_by, build_unit_map(DEFAULT_UNITS), unit_canonical)
        assert (qty, error and error["code"]) == expected

    @pytest.mark.asyncio
    async def test_committed_quantity_and_units_mean_what_the_source_meant(self, client, session, perm):
        await _import_clean(client, perm["admin_h"], [
            {"name": "Loose", "sell_by": "carat", "weight": "1.5", "weight_unit": "carat"},
            {"name": "Parcel", "sell_by": "gram", "quantity": "3", "weight": "1.5", "weight_unit": "carat"},
        ], "units-commit")
        by_name = {s["name"]: s for s in await _item_states(session, perm["company_id"])}
        assert (by_name["Loose"]["quantity"], by_name["Loose"]["sell_by"], by_name["Loose"]["weight"], by_name["Loose"]["weight_unit"]) == (1.5, "carat", 1.5, "carat")
        assert (by_name["Parcel"]["quantity"], by_name["Parcel"]["sell_by"], by_name["Parcel"]["weight"], by_name["Parcel"]["weight_unit"]) == (3.0, "gram", 1.5, "carat")

    @pytest.mark.asyncio
    async def test_carat_weight_is_not_imported_as_grams(self, client, session, perm):
        rows = [{"name": "Stone", "category": "ruby", "weight": "1.5", "weight_unit": "carat"}]
        preview = await _rows_preview(client, perm["admin_h"], rows, key="ct-as-g")
        assert _codes(preview["errors"]) == [(1, "weight", "weight_unit_mismatch")]
        before = await _item_count(session, perm["company_id"])
        r = await _rows_commit(client, perm["admin_h"], rows, key="ct-as-g", preview_hash=preview["preview_hash"])
        assert r.status_code == 422
        assert await _item_count(session, perm["company_id"]) == before

    # INV-UNIT-02 -----------------------------------------------------------

    @pytest.mark.parametrize("header,unit", [
        ("weight_ct", "carat"), ("Carats", "carat"), ("ct", "carat"), ("Weight (ct)", "carat"),
        ("weight_g", "gram"), ("grams", "gram"),
        ("weight_kg", "kg"), ("Kilograms", "kg"),
        ("weight_oz", "oz"), ("ounces", "oz"),
        ("weight_lb", "lb"), ("Pounds", "lb"),
        ("weight", None), ("Gross weight g", None), ("mass", None), ("weight_stone", None),
    ])
    def test_weight_header_table(self, header, unit):
        from celerp_inventory.services import weight_unit_from_header
        assert weight_unit_from_header(header) == unit

    def test_mapped_weight_unit_column_wins_over_the_header(self):
        from celerp_inventory.services import source_header_semantics
        assert source_header_semantics({"weight_ct": "weight", "unit": "weight_unit"}, "USD").weight_unit is None
        assert source_header_semantics({"weight_ct": "weight"}, "USD").weight_unit == "carat"

    @pytest.mark.asyncio
    async def test_weight_header_means_the_same_in_browser_and_file_preview(self, client, perm, write_upload):
        from celerp_inventory.services import apply_source_semantics, source_header_semantics
        from ui.routes.csv_import import apply_column_mapping, form_mapping
        csv_text = "Title,Carats,Unit\nStone,1.5,carat\n"
        mapping = {"Title": "name", "Carats": "weight", "Unit": "sell_by"}

        form = {f"map__{c}": t for c, t in mapping.items()}
        remapped, _cols = apply_column_mapping(form, csv_text)
        import csv as _csv
        import io as _io
        semantics = source_header_semantics(form_mapping(form, list(mapping)), "USD")
        browser_rows = apply_source_semantics(list(_csv.DictReader(_io.StringIO(remapped))), semantics)

        fid = write_upload(perm, csv_text)
        r = await client.post("/items/import/preview", json={"file_id": fid, "mapping": mapping}, headers=perm["admin_h"])
        assert r.status_code == 200, r.text
        assert r.json()["sample"] == browser_rows == [{"name": "Stone", "weight": "1.5", "sell_by": "carat", "weight_unit": "carat"}]
        assert r.json()["errors"] == []

    # INV-PRICE-01 ----------------------------------------------------------

    @pytest.mark.parametrize("header,target,currency,code", [
        ("Retail price", "retail_price", "THB", None),
        ("Price", "retail_price", "THB", None),
        ("Price USD", "retail_price", "USD", None),
        ("price usd", "retail_price", "THB", "price_currency_mismatch"),
        ("Price (USD)", "retail_price", "THB", "price_currency_mismatch"),
        ("Price [usd]", "retail_price", "THB", "price_currency_mismatch"),
        ("Price USD", "retail_price", "THB", "price_currency_mismatch"),
        ("Price $", "retail_price", "THB", None),
        ("Top price", "retail_price", "THB", None),
        ("Price/ct", "retail_price_total", "THB", "price_basis_unsupported"),
        ("Price per dozen", "retail_price", "THB", "price_basis_unsupported"),
        ("Price per unit", "retail_price", "THB", None),
        ("Total cost", "cost_price_total", "THB", None),
        ("Total cost", "cost_price", "THB", "price_total_as_unit"),
    ])
    def test_price_header_matrix(self, header, target, currency, code):
        from celerp_inventory.services import source_header_semantics
        errors = source_header_semantics({header: target}, currency).errors
        assert [e["code"] for e in errors] == ([code] if code else [])

    def test_per_carat_price_carries_its_basis(self):
        from celerp_inventory.services import source_header_semantics
        semantics = source_header_semantics({"Price/ct (THB)": "retail_price"}, "THB")
        assert semantics.errors == [] and semantics.price_basis == {"retail_price": "carat"}

    @pytest.mark.asyncio
    async def test_per_carat_price_imports_only_for_items_sold_by_carat(self, client, session, perm):
        ok = [{"name": "Loose", "sell_by": "carat", "quantity": "2", "retail_price": "100", "retail_price_basis": "carat"}]
        await _import_clean(client, perm["admin_h"], ok, "basis-ok")
        loose = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == "Loose")
        assert loose["retail_price"] == 100.0
        assert "retail_price_basis" not in (loose.get("attributes") or {})

        blocked = [{"name": "Set", "sell_by": "piece", "quantity": "1", "retail_price": "100", "retail_price_basis": "carat"}]
        assert _codes((await _rows_preview(client, perm["admin_h"], blocked, key="basis-no"))["errors"]) == [(1, "retail_price", "price_basis_mismatch")]

    @pytest.mark.asyncio
    async def test_foreign_currency_file_column_blocks_the_file_commit(self, client, session, perm, write_upload):
        await _set_company_settings(session, perm["company_id"], currency="THB")
        fid = write_upload(perm, "name,sell_by,quantity,Price (USD)\nWidget,piece,1,10\n")
        mapping = {"name": "name", "sell_by": "sell_by", "quantity": "quantity", "Price (USD)": "retail_price"}
        preview = (await client.post("/items/import/preview", json={"file_id": fid, "mapping": mapping}, headers=perm["admin_h"])).json()
        assert [(e["field"], e["code"]) for e in preview["errors"]] == [("Price (USD)", "price_currency_mismatch")]
        before = await _item_count(session, perm["company_id"])
        r = await client.post("/items/import/commit", json={
            "file_id": fid, "mapping": mapping, "preview_hash": preview["preview_hash"],
        }, headers=perm["admin_h"])
        assert r.status_code == 422
        assert await _item_count(session, perm["company_id"]) == before

    @pytest.mark.asyncio
    async def test_total_cost_keeps_total_semantics(self, client, session, perm):
        await _import_clean(client, perm["admin_h"], [{"name": "Lot", "sell_by": "piece", "quantity": "4", "cost_price_total": "100"}], "total-cost")
        lot = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == "Lot")
        assert lot["cost_total"] == 100.0

    # Gemstone acceptance oracle (plan 9.6) ----------------------------------

    @pytest.mark.asyncio
    async def test_gemstone_file_imports_with_its_meaning_or_is_blocked(self, client, session, perm, write_upload):
        await _set_company_settings(session, perm["company_id"], currency="THB",
                                    category_schemas=_GEM_CATEGORIES, category_display_names=_GEM_NAMES)
        csv_text = "Stone,Type,Carats,Sell by,Price/ct (THB),Price (USD)\nPigeon,Ruby,1.5,carat,1000,700\n"
        base = {"Stone": "name", "Type": "category", "Carats": "weight", "Sell by": "sell_by", "Price/ct (THB)": "retail_price"}

        fid = write_upload(perm, csv_text)
        blocked = {**base, "Price (USD)": "cost_price"}
        errors = (await client.post("/items/import/preview", json={"file_id": fid, "mapping": blocked}, headers=perm["admin_h"])).json()["errors"]
        assert [(e["field"], e["code"]) for e in errors] == [("Price (USD)", "price_currency_mismatch")]

        clean = {**base, "Price (USD)": "__skip__"}
        preview = (await client.post("/items/import/preview", json={"file_id": fid, "mapping": clean}, headers=perm["admin_h"])).json()
        assert preview["errors"] == []
        r = await client.post("/items/import/commit", json={"file_id": fid, "mapping": clean, "preview_hash": preview["preview_hash"]}, headers=perm["admin_h"])
        assert r.status_code == 200 and r.json()["created"] == 1, r.text
        stone = next(s for s in await _item_states(session, perm["company_id"]) if s["name"] == "Pigeon")
        assert (stone["category"], stone["quantity"], stone["weight"], stone["weight_unit"], stone["sell_by"], stone["retail_price"]) == (
            "ruby", 1.5, 1.5, "carat", "carat", 1000.0,
        )

    @pytest.mark.asyncio
    async def test_browser_mapping_blocks_a_foreign_currency_price_column(self, stage_dir):
        from ui.app import app as ui_app
        ref = ci._write_stage(_COMPANY_A, "name,sell_by,Price (USD)\nWidget,piece,10\n")
        company = {"id": _COMPANY_A, "currency": "THB", "current_role": "owner", "settings": {}}
        form = {"csv_ref": ref, "map__name": "name", "map__sell_by": "sell_by", "map__Price (USD)": "retail_price"}
        preview = AsyncMock()
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
             patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}])), \
             patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})), \
             patch("ui.api_client.preview_import_rows", new=preview):
            async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                r = await c.post("/inventory/import/mapped", data=form, cookies=_owner_cookies())
        assert r.status_code == 200, r.text
        assert "Price (USD)" in r.text and "different currency" in r.text
        preview.assert_not_awaited()
