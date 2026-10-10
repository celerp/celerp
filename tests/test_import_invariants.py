# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Fast invariant suite for import and for the setup writer.

Each test class owns one family of invariants; each test asserts a single
property that must hold for every import path, not one screen's behavior.
"""

from __future__ import annotations

import ast
import io
import json
import re
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from celerp.services import import_stage
from httpx import ASGITransport, AsyncClient

from fasthtml.common import to_xml

from test_helpers import make_test_token
from ui.i18n import t as t_
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
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        assert import_stage.REF_RE.fullmatch(ref)
        assert import_stage.stage_paths(ref) is not None
        assert import_stage.stage_paths(ref.upper()) is None

    @pytest.mark.parametrize("bad", [
        "../etc/passwd", "imp_../../x", "..", "imp_" + "0" * 30 + "/..",
    ])
    def test_import_ref_rejects_path_traversal_before_filesystem_access(self, bad, monkeypatch):
        def _boom():
            raise AssertionError("filesystem resolver reached for an invalid ref")
        monkeypatch.setattr(import_stage, "stage_dir", _boom)
        assert import_stage.stage_paths(bad) is None
        assert import_stage.read_stage(_COMPANY_A, bad) is None
        import_stage.delete_ref(bad)

    @pytest.mark.parametrize("bad", [
        "/tmp/imp_" + "0" * 32, "imp_" + "0" * 31 + "\\", "C:\\imp_" + "0" * 32,
    ])
    def test_import_ref_rejects_absolute_and_backslash_paths(self, bad):
        assert import_stage.stage_paths(bad) is None

    @pytest.mark.parametrize("bad", [
        "", "0" * 32, "imp_" + "0" * 31, "imp_" + "0" * 33, "xmp_" + "0" * 32,
        "imp_" + "0" * 32 + "\n", " imp_" + "0" * 32, "imp_" + "g" * 32, "imp_" + "0" * 4096,
    ])
    def test_import_ref_rejects_prefix_suffix_and_oversize_tokens(self, bad):
        assert import_stage.stage_paths(bad) is None

    def test_import_refs_do_not_collide(self, stage_dir):
        refs = {import_stage.write_stage(_COMPANY_A, "x") for _ in range(500)}
        assert len(refs) == 500

    def test_import_stage_same_company_loads(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        assert import_stage.read_stage(_COMPANY_A, ref) == "sku\nA\n"

    def test_import_stage_wrong_company_fails_closed(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        assert import_stage.read_stage(_COMPANY_B, ref) is None
        assert import_stage.read_stage("", ref) is None

    def test_import_stage_missing_metadata_fails_closed(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        (stage_dir / f"{ref}.meta").unlink()
        assert import_stage.read_stage(_COMPANY_A, ref) is None

    def test_import_stage_requires_company(self, stage_dir):
        with pytest.raises(ValueError):
            import_stage.write_stage("", "sku\nA\n")

    @pytest.mark.asyncio
    async def test_authenticated_load_uses_the_callers_company(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_B})):
            assert await ci.load_import_csv("tok", ref) is None
            assert await ci.resolve_import_csv("tok", {"csv_ref": ref}) == ""
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A})):
            assert await ci.resolve_import_csv("tok", {"csv_ref": ref}) == "sku\nA\n"

    def test_expired_stage_is_rejected_and_removed(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        meta = stage_dir / f"{ref}.meta"
        stale = time.time() - import_stage.MAX_AGE_SECONDS - 1
        meta.write_text(json.dumps({"company_id": _COMPANY_A, "created_at": stale}))
        assert import_stage.read_stage(_COMPANY_A, ref) is None
        assert not meta.exists() and not (stage_dir / f"{ref}.csv").exists()

    def test_cleanup_never_removes_recent_stage(self, stage_dir):
        fresh = import_stage.write_stage(_COMPANY_A, "fresh")
        old = import_stage.write_stage(_COMPANY_A, "old")
        stale = time.time() - import_stage.MAX_AGE_SECONDS - 1
        (stage_dir / f"{old}.meta").write_text(json.dumps({"company_id": _COMPANY_A, "created_at": stale}))
        assert import_stage.cleanup_expired() == 1
        assert import_stage.read_stage(_COMPANY_A, fresh) == "fresh"

    def test_no_route_constructs_a_staging_path(self):
        """Only the staging service names the stage directory or resolves stage paths."""
        offenders = []
        for path in [*(_REPO / "ui").rglob("*.py"), *(_REPO / "celerp").rglob("*.py")]:
            if path.name == "import_stage.py":
                continue
            src = path.read_text(encoding="utf-8")
            if "import_staging" in src:
                offenders.append(f"{path}: import_staging")
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Attribute) and node.attr == "stage_dir":
                    offenders.append(f"{path}:{node.lineno}: stage_dir")
        assert not offenders, offenders

    async def _confirm(self, stage_dir, import_rows):
        from ui.app import app as ui_app
        ref = import_stage.write_stage(_COMPANY_A, "sku,name,sell_by\nA-1,Ruby,piece\n")
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
        assert import_stage.read_stage(_COMPANY_A, ref) is None

    @pytest.mark.asyncio
    async def test_failed_commit_keeps_stage_for_retry(self, stage_dir):
        from ui.api_client import APIError
        failing = AsyncMock(side_effect=APIError(500, "boom"))
        ref = await self._confirm(stage_dir, failing)
        assert import_stage.read_stage(_COMPANY_A, ref) is not None


# ---------------------------------------------------------------------------
# INV-SETUP-01..04 - setup delegates to the canonical business-type operation,
# and only reports success once the company is durably marked as being set up.
# ---------------------------------------------------------------------------


def _owner_cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


def _setup_form(vertical: str | None = "gemstones") -> dict:
    form = {"currency": "USD", "timezone": "UTC"}
    if vertical is not None:
        form["vertical"] = vertical
    return form

def _bridged_client(token, timeout=10.0):
    """UI-to-API client that reaches the real API app in process."""
    from celerp.main import app as api_app
    return AsyncClient(transport=ASGITransport(app=api_app), base_url="http://test",
                       headers={"Authorization": f"Bearer {token}"}, follow_redirects=True)


async def _real_ui(token: str, method: str, path: str, *, patch_company=None, **kwargs):
    """One UI request whose API calls reach the real API (restarts are never performed)."""
    from ui.app import app as ui_app
    import ui.api_client as api
    with patch("ui.api_client._client", _bridged_client), \
         patch("ui.api_client.restart_system", new=AsyncMock(return_value={})), \
         patch("ui.api_client.patch_company", new=patch_company or api.patch_company):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            return await c.request(method, path, cookies={"celerp_token": token}, **kwargs)


async def _settings(client, h) -> dict:
    return (await client.get("/companies/me", headers=h)).json()["settings"]


def _token_of(h: dict) -> str:
    return h["Authorization"].split()[1]


_RETRYABLE_ERROR = 'class="flash flash--error"'


class TestSetupWriterInvariant:
    def test_every_visible_preset_enables_celerp_verticals(self):
        from celerp.services.vertical_presets import installed_preset_modules, list_presets
        from ui.routes.setup import business_type_options
        visible = list_presets()
        assert {p["name"] for p in visible} == {v for v, _ in business_type_options()}
        for preset in visible:
            assert "celerp-verticals" in installed_preset_modules(preset), preset["name"]

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
    @pytest.mark.parametrize("transport", ["setup", "settings_api"])
    async def test_setup_finalize_exact_retry_has_same_company_state(self, client, transport):
        h = await _register(client)
        states = []
        for _ in range(2):
            if transport == "setup":
                r = await _real_ui(_token_of(h), "POST", "/setup/company", data=_setup_form())
                assert r.status_code == 302, r.text
                states.append(await _settings(client, h))
            else:
                states.append(await _apply(client, h, "gemstones"))
        assert states[0] == states[1]

    @pytest.mark.asyncio
    async def test_setup_finalize_second_success_does_not_duplicate_category_or_unit_state(self, client, session):
        h = await _register(client)
        company_id = (await client.get("/companies/me", headers=h)).json()["id"]
        snapshots = []
        for _ in range(2):
            r = await _real_ui(_token_of(h), "POST", "/setup/company", data=_setup_form())
            assert r.status_code == 302, r.text
            snapshots.append((await _settings(client, h), await _item_count(session, company_id)))
        (first, items_first), (second, items_second) = snapshots
        assert items_second == items_first
        for settings in (first, second):
            names = [u["name"] for u in settings.get("units") or []]
            assert len(names) == len(set(names)), names
            for slug, schema in settings["category_schemas"].items():
                fields = schema if isinstance(schema, list) else schema.get("fields") or []
                keys = [f["key"] for f in fields if isinstance(f, dict) and "key" in f]
                assert len(keys) == len(set(keys)), (slug, keys)
        for key in ("units", "category_schemas", "category_display_names"):
            assert second.get(key) == first.get(key), key

    @pytest.mark.asyncio
    async def test_setup_retry_after_settings_write_failure_applies_once(self, client, session):
        import ui.api_client as api
        from ui.api_client import APIError
        h = await _register(client)
        company_id = (await client.get("/companies/me", headers=h)).json()["id"]
        before = await _settings(client, h)
        items_before = await _item_count(session, company_id)

        async def _unreachable(token, data):
            raise APIError(503, "The server could not be reached.")
        first = await _real_ui(_token_of(h), "POST", "/setup/company", data=_setup_form(), patch_company=_unreachable)
        assert first.status_code == 200 and "location" not in first.headers
        assert _RETRYABLE_ERROR in first.text
        assert (await _settings(client, h)).get("vertical") == before.get("vertical")

        retried = await _real_ui(_token_of(h), "POST", "/setup/company", data=_setup_form(),
                                 patch_company=api.patch_company)
        assert retried.status_code == 302
        assert retried.headers["location"] in ("/dashboard", "/setup/activating")
        assert (await _settings(client, h))["vertical"] == "gemstones"
        assert await _item_count(session, company_id) >= items_before
        root = await _real_ui(_token_of(h), "GET", "/")
        assert root.headers["location"] == "/dashboard"

    @pytest.mark.asyncio
    async def test_settings_business_type_applies_after_setup(self, client):
        h = await _register(client)
        # The Settings editor and the API both apply the preset at any time after setup.
        r = await _real_ui(_token_of(h), "PATCH", "/settings/company/vertical", data={"value": "fashion"})
        assert r.status_code == 200, r.text
        assert (await _settings(client, h))["vertical"] == "fashion"
        settings = await _apply(client, h, "gemstones")
        assert settings["vertical"] == "gemstones"
        from celerp.services.vertical_presets import load_preset
        assert set(load_preset("gemstones")["categories"]) <= set(settings["category_schemas"])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["admin", "manager", "operator", "viewer"])
    async def test_setup_company_without_permission_is_denied(self, client, session, role):
        from test_helpers import invite_user
        owner_h = await _register(client)
        before = await _settings(client, owner_h)
        member = await invite_user(client, session, owner_h, f"{role}-{uuid.uuid4().hex[:6]}@setup.example", role)
        denied = await _real_ui(member, "POST", "/setup/company", data=_setup_form())
        assert denied.status_code == 200 and "location" not in denied.headers
        assert _RETRYABLE_ERROR in denied.text
        after = await _settings(client, owner_h)
        assert after.get("vertical") == before.get("vertical")
        assert after.get("category_schemas") == before.get("category_schemas")
        # The same request from the owner, who holds the permission, goes through.
        allowed = await _real_ui(_token_of(owner_h), "POST", "/setup/company", data=_setup_form())
        assert allowed.status_code == 302, allowed.text
        assert (await _settings(client, owner_h))["vertical"] == "gemstones"


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
        build = await build_import_records(session, cid, [{"name": "Stone", "category": "diamond"}], upsert=False)
        assert build.errors == []
        assert build.records[0]["data"]["sell_by"] == "gram"

    @pytest.mark.asyncio
    async def test_no_sell_by_and_no_default_is_rejected_on_the_row(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Widget", "pieces": "1"}], upsert=False)
        assert build.records == []
        assert [(e["row"], e["field"], e["code"]) for e in build.errors] == [(1, "sell_by", "sell_by_unresolved")]

    @pytest.mark.asyncio
    async def test_unknown_sell_by_is_rejected_on_the_row(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Widget", "sell_by": "furlong", "pieces": "1"}], upsert=False)
        assert [(e["field"], e["code"]) for e in build.errors] == [("sell_by", "sell_by_invalid")]

    @pytest.mark.asyncio
    async def test_explicit_valid_sell_by_succeeds(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        build = await build_import_records(session, cid, [{"name": "Widget", "sell_by": "Piece", "pieces": "1"}], upsert=False)
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


async def _business_snapshot(session, company_id: str) -> dict:
    """Every business effect an item import can have, read fresh from the database."""
    from sqlalchemy import select

    from celerp.models.company import Company, Location
    from celerp.models.import_batch import ImportBatch
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    cid = uuid.UUID(company_id)
    session.expire_all()
    projections = (await session.execute(select(Projection).where(Projection.company_id == cid))).scalars().all()
    items = {p.entity_id: (p.state, p.version, p.location_id, p.updated_at) for p in projections if p.entity_type == "item"}
    ledger = sorted(
        (e.id, e.entity_id, e.event_type, e.idempotency_key)
        for e in (await session.execute(select(LedgerEntry).where(LedgerEntry.company_id == cid))).scalars().all()
    )
    batches = sorted(
        (str(b.id), b.row_count, b.status, tuple(b.entity_ids))
        for b in (await session.execute(select(ImportBatch).where(ImportBatch.company_id == cid))).scalars().all()
    )
    locations = sorted(
        loc.name for loc in (await session.execute(select(Location).where(Location.company_id == cid))).scalars().all()
    )
    company = await session.get(Company, cid)
    return {
        "item_count": len(items),
        "items": items,
        "quantities": {eid: v[0].get("quantity") for eid, v in items.items()},
        "projections": sorted((p.entity_type, p.entity_id, p.version) for p in projections),
        "ledger_count": len(ledger),
        "ledger": ledger,
        "import_batches": batches,
        "locations": locations,
        "category_schemas": (company.settings or {}).get("category_schemas"),
    }


async def _rows_preview(client, h, rows, *, upsert=False, key="op-1", decisions=None) -> dict:
    r = await client.post("/items/import/rows/preview", json={
        "rows": rows, "upsert": upsert, "idempotency_key": key, "decisions": decisions or {},
    }, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _rows_commit(client, h, rows, *, upsert=False, key="op-1", preview_hash=None, decisions=None):
    return await client.post("/items/import/rows", json={
        "rows": rows, "upsert": upsert, "idempotency_key": key, "preview_hash": preview_hash,
        "decisions": decisions or {},
    }, headers=h)


def _lots(rows) -> dict:
    """Keep every SKU that repeats in ``rows`` as separate lots."""
    skus = [r.get("sku") for r in rows if r.get("sku")]
    return {"separate_lots": sorted({s for s in skus if skus.count(s) > 1})}


async def _seed_items(client, h, rows, key):
    r = await _rows_commit(client, h, rows, key=key, decisions=_lots(rows))
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
        real_plan, real_writer = routes.build_import_plan, routes.import_items

        async def plan_spy(*a, **k):
            order.append("plan")
            return await real_plan(*a, **k)

        async def writer_spy(*a, **k):
            order.append("writer")
            return await real_writer(*a, **k)

        rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1"}]
        ph = (await _rows_preview(client, perm["admin_h"], rows))["preview_hash"]
        monkeypatch.setattr(routes, "build_import_plan", plan_spy)
        monkeypatch.setattr(routes, "import_items", writer_spy)
        r = await _rows_commit(client, perm["admin_h"], rows, preview_hash=ph)
        assert r.status_code == 200, r.text
        assert order == ["plan", "writer"]

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
        spy = AsyncMock(wraps=routes.build_import_plan)
        monkeypatch.setattr(routes, "build_import_plan", spy)
        await _rows_preview(client, perm["admin_h"], [{"name": "Widget", "sell_by": "piece"}])
        assert spy.await_count == 1

    @pytest.mark.asyncio
    async def test_file_preview_delegates_to_semantic_row_preview(self, client, perm, monkeypatch, write_upload):
        import celerp_inventory.routes as routes
        spy = AsyncMock(wraps=routes.build_import_plan)
        monkeypatch.setattr(routes, "build_import_plan", spy)
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

    @staticmethod
    def _hash(rows, upsert=False, key="k", fingerprint="f", decisions=None):
        from types import SimpleNamespace

        from celerp_inventory.routes import _rows_preview_hash
        plan = SimpleNamespace(decisions=decisions or {"exclude": []}, semantic_fingerprint=fingerprint)
        return _rows_preview_hash(rows, upsert, key, plan)

    def test_preview_hash_changes_when_row_changes(self):
        assert self._hash([{"name": "A"}]) != self._hash([{"name": "B"}])

    def test_preview_hash_changes_when_upsert_changes(self):
        assert self._hash([{"name": "A"}]) != self._hash([{"name": "A"}], upsert=True)

    def test_preview_hash_changes_when_operation_key_changes(self):
        assert self._hash([{"name": "A"}], key="k1") != self._hash([{"name": "A"}], key="k2")

    def test_preview_hash_is_stable_for_equivalent_dict_key_order(self):
        assert self._hash([{"name": "A", "sku": "1"}]) == self._hash([{"sku": "1", "name": "A"}])

    def test_preview_hash_changes_when_semantic_fingerprint_changes(self):
        assert self._hash([{"name": "A"}], fingerprint="f1") != self._hash([{"name": "A"}], fingerprint="f2")

    def test_preview_hash_changes_when_row_decisions_change(self):
        assert self._hash([{"name": "A"}], decisions={"exclude": []}) != self._hash([{"name": "A"}], decisions={"exclude": [1]})

    @pytest.mark.asyncio
    async def test_file_preview_hash_changes_when_file_bytes_change(self, client, perm, write_upload):
        from celerp.ai.files import upload_dir
        fid = write_upload(perm, "name,sell_by\nWidget,piece\n")
        ha = (await client.get(f"/items/import/preview?file_id={fid}", headers=perm["admin_h"])).json()["preview_hash"]
        # Same file id and size, different content.
        (upload_dir() / f"{fid}.bin").write_bytes(b"name,sell_by\nWidget,pound\n")
        hb = (await client.get(f"/items/import/preview?file_id={fid}", headers=perm["admin_h"])).json()["preview_hash"]
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
    async def test_file_preview_asks_for_an_unclear_header_row_then_reads_the_chosen_one(self, client, perm, write_upload):
        fid = write_upload(perm, "Report\nalpha,beta\nsku,name\nA1,Ring\n")
        r = await client.get(f"/items/import/preview?file_id={fid}", headers=perm["admin_h"])
        assert r.status_code == 200, r.text  # the line naming known columns is found
        assert r.json()["header_row"] == 2
        fid = write_upload(perm, "Report\nalpha,beta\nA1,Ring\n")
        r = await client.get(f"/items/import/preview?file_id={fid}", headers=perm["admin_h"])
        assert r.status_code == 422, r.text
        detail = r.json()["detail"]
        assert detail["code"] == "header_row_required" and detail["lines"][:2] == [["Report"], ["alpha", "beta"]]
        r = await client.get(f"/items/import/preview?file_id={fid}&header_row=1", headers=perm["admin_h"])
        assert r.status_code == 200, r.text
        assert r.json()["header_row"] == 1

    @pytest.mark.asyncio
    async def test_file_preview_hash_changes_when_header_row_changes(self, client, perm, write_upload):
        fid = write_upload(perm, "name,sku\nname,sku\nRing,A1\n")
        ha = (await client.get(f"/items/import/preview?file_id={fid}&header_row=0", headers=perm["admin_h"])).json()
        hb = (await client.get(f"/items/import/preview?file_id={fid}&header_row=1", headers=perm["admin_h"])).json()
        assert ha["preview_hash"] != hb["preview_hash"]

    @pytest.mark.asyncio
    async def test_file_preview_hash_changes_when_sheet_changes(self, client, perm, write_upload, monkeypatch):
        import celerp.importers.tabular as tabular
        monkeypatch.setattr(tabular, "read_table_at_header",
                            lambda data, filename, sheet=None, header_row=None, known=(): (["name"], [{"name": "Widget"}], 0))
        fid = write_upload(perm, "not really a workbook", filename="items.xlsx")
        ha = (await client.get(f"/items/import/preview?file_id={fid}&sheet=One", headers=perm["admin_h"])).json()["preview_hash"]
        hb = (await client.get(f"/items/import/preview?file_id={fid}&sheet=Two", headers=perm["admin_h"])).json()["preview_hash"]
        assert ha != hb

    # INV-IMPORT-02: one logical import is semantically built once ------------

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)  # 1001 rows in three chunks; slower than the suite guard allows on a shared runner
    async def test_inv_import_02_large_import_is_built_once_and_written_in_bounded_chunks(self, client, session, perm, monkeypatch):
        import celerp_inventory.services as svc
        events: list[tuple] = []
        real_build, real_write = svc.build_import_records, svc.write_import_batch

        async def build_spy(*a, **k):
            events.append(("build", len(a[2])))
            return await real_build(*a, **k)

        async def write_spy(session, company_id, user, role, settings, body, **kwargs):
            events.append(("write", len(body.records)))
            return await real_write(session, company_id, user, role, settings, body, **kwargs)

        # Rows 1 and 1001 share a SKU no existing item carries, under update-existing.
        # Resolved once against the pre-commit state, both are creates. Were row 1001
        # resolved after the first chunk's writes, it would find row 1's item as its
        # unique SKU match and patch it instead.
        rows = [{"name": "Shared first", "sku": "INV02-SHARED", "sell_by": "piece", "quantity": "1"}]
        rows += [{"name": f"Item {i}", "sell_by": "piece", "quantity": "1"} for i in range(2, 1001)]
        rows.append({"name": "Shared last", "sku": "INV02-SHARED", "sell_by": "piece", "quantity": "2"})
        assert len(rows) == 1001
        preview = await _rows_preview(client, perm["admin_h"], rows, upsert=True, key="big", decisions=_lots(rows))
        assert preview["errors"] == []
        monkeypatch.setattr(svc, "build_import_records", build_spy)
        monkeypatch.setattr(svc, "write_import_batch", write_spy)
        r = await _rows_commit(client, perm["admin_h"], rows, upsert=True, key="big", preview_hash=preview["preview_hash"],
                               decisions=_lots(rows))
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["created"], body["updated"], body["errors"]) == (1001, 0, [])
        assert events == [("build", 1001), ("write", 500), ("write", 500), ("write", 1)]
        states = await _item_states(session, perm["company_id"])
        assert len([s for s in states if s["name"].startswith(("Item ", "Shared "))]) == 1001
        shared = sorted((s["name"], float(s["quantity"])) for s in states if s.get("sku") == "INV02-SHARED")
        assert shared == [("Shared first", 1.0), ("Shared last", 2.0)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bound", [True, False])
    async def test_inv_import_02_import_creating_a_location_is_built_once(self, client, session, perm, monkeypatch, bound):
        from sqlalchemy import select

        import celerp_inventory.services as svc
        from celerp.models.company import Location
        events: list[tuple] = []
        real_build, real_write = svc.build_import_records, svc.write_import_batch

        async def build_spy(*a, **k):
            events.append(("build", len(a[2])))
            return await real_build(*a, **k)

        async def write_spy(session, company_id, user, role, settings, body, **kwargs):
            events.append(("write", len(body.records)))
            return await real_write(session, company_id, user, role, settings, body, **kwargs)

        rows = [
            {"name": "Annexed one", "sell_by": "piece", "quantity": "1", "location_name": "Annex"},
            {"name": "Annexed two", "sell_by": "piece", "quantity": "1", "location_name": "Annex"},
        ]
        preview = await _rows_preview(client, perm["admin_h"], rows, key="annex")
        assert (preview["errors"], preview["locations_to_create"]) == ([], ["Annex"])
        monkeypatch.setattr(svc, "build_import_records", build_spy)
        monkeypatch.setattr(svc, "write_import_batch", write_spy)
        r = await _rows_commit(
            client, perm["admin_h"], rows, key="annex",
            preview_hash=preview["preview_hash"] if bound else None,
        )
        assert r.status_code == 200, r.text
        assert (r.json()["created"], r.json()["errors"]) == (2, [])
        assert events == [("build", 2), ("write", 2)]
        [annex_id] = (await session.execute(
            select(Location.id).where(Location.company_id == uuid.UUID(perm["company_id"]), Location.name == "Annex")
        )).scalars().all()
        states = await _item_states(session, perm["company_id"])
        assert sorted(s["location_id"] for s in states if s["name"].startswith("Annexed")) == [str(annex_id)] * 2

    # INV-IMPORT-01: an exact retry changes no business state ----------------

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["create", "upsert"])
    async def test_inv_import_01_exact_retry_changes_no_business_state(self, client, session, perm, mode):
        h = perm["admin_h"]
        if mode == "create":
            upsert = False
            # Identical rows are distinct lots; one row also creates a location and
            # grows a category schema from an attribute column.
            rows = [{"name": "Same", "sell_by": "piece", "quantity": "1"}] * 2 + [
                {"name": "Annexed", "sku": "RT-N", "category": "red_a", "sell_by": "piece", "quantity": "4",
                 "retail_price": "12", "location_name": "Annex", "finish": "matte"},
            ]
        else:
            upsert = True
            await _seed_items(client, h, [
                {"name": "Seed A", "sku": "RT-A", "sell_by": "piece", "quantity": "1"},
                {"name": "Seed B", "sku": "RT-B", "sell_by": "piece", "quantity": "2", "cost_price": "5"},
            ], key="retry-seed")
            rows = [
                {"name": "Seed A renamed", "sku": "RT-A", "sell_by": "piece", "quantity": "3", "retail_price": "12"},
                {"name": "Seed B", "sku": "RT-B", "cost_price": "7"},
                {"name": "New C", "sku": "RT-C", "sell_by": "piece", "quantity": "5"},
            ]
        key = f"retry-{mode}"
        ph = (await _rows_preview(client, h, rows, upsert=upsert, key=key))["preview_hash"]

        before = await _business_snapshot(session, perm["company_id"])
        first = await _rows_commit(client, h, rows, upsert=upsert, key=key, preview_hash=ph)
        assert first.status_code == 200 and first.json()["errors"] == [], first.text
        after_first = await _business_snapshot(session, perm["company_id"])
        again = await _rows_commit(client, h, rows, upsert=upsert, key=key, preview_hash=ph)
        assert again.status_code == 200, again.text
        after_second = await _business_snapshot(session, perm["company_id"])

        # The first commit did real work: new lots, and for the upsert the new lot with
        # the inventory account it is booked into, two patches, a cost restatement and
        # the entry booking that change in value (created and posted).
        new_items = set(after_first["items"]) - set(before["items"])
        if mode == "create":
            assert len(new_items) == 3
            assert after_first["locations"] == sorted(before["locations"] + ["Annex"])
            assert "finish" in str(after_first["category_schemas"]["red_a"])
        else:
            assert len(new_items) == 1
            assert after_first["ledger_count"] == before["ledger_count"] + 7
        for part in ("item_count", "items", "quantities", "projections", "ledger_count", "ledger",
                     "import_batches", "locations", "category_schemas"):
            assert after_second[part] == after_first[part], part

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
             patch("ui.api_client.plan_import_rows", new=preview), \
             patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[])), \
             patch("ui.api_client.import_rows", new=import_rows):
            async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                r = await c.post(path, data=data, cookies=_owner_cookies())
        assert r.status_code == 200, r.text
        return r.text, import_rows

    @pytest.mark.asyncio
    async def test_review_with_row_errors_offers_no_import(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "name,quantity\nWidget,1\n")
        preview = AsyncMock(return_value={"errors": [{"row": 1, "field": "sell_by", "code": "sell_by_unresolved", "message": "No selling unit"}],
                                          "locations_to_create": [], "preview_hash": "d" * 64})
        html, _ = await self._ui_post("/inventory/import/review", {"csv_ref": ref}, preview=preview)
        assert "No selling unit" in html
        assert "d" * 64 not in html
        assert 'hx-post="/inventory/import/confirm"' not in html

    @pytest.mark.asyncio
    async def test_changing_update_existing_reruns_review(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "name,sku,sell_by\nWidget,W-1,piece\n")
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "e" * 64})
        html, _ = await self._ui_post("/inventory/import/review", {"csv_ref": ref, "revision": "1", "upsert": "1"},
                                      preview=preview)
        assert preview.await_args.kwargs["upsert"] is True
        assert 'name="preview_hash" value="' + "e" * 64 in html
        assert 'hx-post="/inventory/import/review"' in html

    @pytest.mark.asyncio
    async def test_confirm_without_reviewed_hash_never_imports(self, stage_dir):
        ref = import_stage.write_stage(_COMPANY_A, "name,sell_by\nWidget,piece\n")
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "f" * 64})
        html, writer = await self._ui_post("/inventory/import/confirm", {"csv_ref": ref}, preview=preview)
        writer.assert_not_awaited()
        assert "f" * 64 in html

    @pytest.mark.asyncio
    async def test_stale_confirm_returns_to_review(self, stage_dir):
        from ui.api_client import APIError
        ref = import_stage.write_stage(_COMPANY_A, "name,sell_by\nWidget,piece\n")
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "a" * 64})
        stale = AsyncMock(side_effect=APIError(409, {"code": "preview_stale"}))
        html, _ = await self._ui_post("/inventory/import/confirm", {"csv_ref": ref, "preview_hash": "b" * 64},
                                      preview=preview, import_rows=stale)
        assert "a" * 64 in html
        assert import_stage.read_stage(_COMPANY_A, ref) is not None


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

    @pytest.mark.parametrize("value", ["Farbedelstein", "farbedelstein", "Colored Stone", "colored_stone"])
    def test_library_category_matches_its_name_in_every_language(self, value):
        from celerp_inventory.services import resolve_import_category
        assert resolve_import_category(value, ["colored_stone"], {"colored_stone": "Colored Stone"}) == ("colored_stone", None)

    def test_library_category_with_no_stored_name_matches_its_translation(self):
        from celerp_inventory.services import resolve_import_category
        assert resolve_import_category("Farbedelstein", ["colored_stone"], {}) == ("colored_stone", None)

    def test_a_decomposed_name_matches_its_category(self):
        """A file can spell "ä" as "a" plus a combining mark (NFD); it is the same name.
        Red statement: the decomposed name was kept as a new category."""
        import unicodedata
        from celerp_inventory.services import resolve_import_category
        value = unicodedata.normalize("NFD", "Getränk (alkoholfrei)")
        assert resolve_import_category(value, ["beverage_nonalc"], {}) == ("beverage_nonalc", None)

    def test_an_unmatched_decomposed_name_is_kept_composed(self):
        import unicodedata
        from celerp_inventory.services import resolve_import_category
        assert resolve_import_category(unicodedata.normalize("NFD", "Käse"), [], {}) == ("Käse", None)

    def test_renamed_category_matches_only_its_own_name(self):
        from celerp_inventory.services import resolve_import_category
        names = {"colored_stone": "Stones"}
        assert resolve_import_category("Farbedelstein", ["colored_stone"], names) == ("Farbedelstein", None)
        assert resolve_import_category("stones", ["colored_stone"], names) == ("colored_stone", None)

    @pytest.mark.asyncio
    async def test_translated_category_name_imports_into_the_existing_category(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        await _set_company_settings(session, cid, category_schemas={"colored_stone": []}, category_display_names={"colored_stone": "Colored Stone"})
        build = await build_import_records(session, cid, [{"name": "A", "category": "Farbedelstein", "sell_by": "piece"}], upsert=False)
        assert build.errors == []
        assert build.records[0]["data"]["category"] == "colored_stone"

    @pytest.mark.asyncio
    async def test_label_and_slug_drive_the_same_default_unit_and_key(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        await _set_company_settings(session, cid, category_schemas=_GEM_CATEGORIES, category_display_names=_GEM_NAMES)
        build = await build_import_records(session, cid, [
            {"name": "A", "category": "Ruby", "quantity": "1"},
            {"name": "B", "category": "ruby", "quantity": "1"},
        ], upsert=False)
        assert build.errors == []
        assert [(r["data"]["category"], r["data"]["sell_by"]) for r in build.records] == [("ruby", "gram"), ("ruby", "gram")]

    @pytest.mark.asyncio
    async def test_unknown_category_is_kept_as_custom(self, session):
        from celerp_inventory.services import build_import_records
        cid = await _seed_company(session)
        await _set_company_settings(session, cid, category_schemas=_GEM_CATEGORIES, category_display_names=_GEM_NAMES)
        build = await build_import_records(session, cid, [{"name": "A", "category": "Opal", "sell_by": "piece"}], upsert=False)
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

    @pytest.mark.asyncio
    async def test_category_only_items_carry_matches_without_a_second_category(self, client, session, perm):
        """A category that lives only on items (never in the category settings) is a
        category too: an import naming it in another case lands in it. Red statement:
        /items/categories listed both "Gem" and "gem"."""
        h = perm["admin_h"]
        r = await client.post("/items", headers=h, json={"sku": "GEM-1", "name": "Gem one", "quantity": 1,
                                                         "sell_by": "piece", "category": "Gem"})
        assert r.status_code in (200, 201), r.text
        await _import_clean(client, h, [{"name": "Gem two", "category": "gem", "sell_by": "piece", "quantity": "1"}], "cat-item-only")
        cats = (await client.get("/items/categories", headers=h)).json()
        assert "gem" not in cats and "Gem" in cats, cats
        states = await _item_states(session, perm["company_id"])
        assert sorted(s.get("category") for s in states if s.get("name", "").startswith("Gem ")) == ["Gem", "Gem"]

    @staticmethod
    def _batch(category: str, key: str) -> dict:
        return {"records": [{"entity_id": f"item:{key}", "event_type": "item.created", "source": "csv",
                             "idempotency_key": key,
                             "data": {"sku": key.upper(), "name": "Batch", "quantity": 1, "sell_by": "piece",
                                      "category": category}}]}

    @pytest.mark.asyncio
    async def test_batch_import_resolves_a_translated_category_name(self, client, session, perm):
        """/items/import/batch resolves a category the way the rows import does. Red
        statement: a batch row naming "Bier" was stored as the category "Bier"."""
        await _set_company_settings(session, perm["company_id"], category_schemas={"beer": []},
                                    category_display_names={"beer": "Beer"})
        r = await client.post("/items/import/batch", headers=perm["admin_h"], json=self._batch("Bier", "batch-bier"))
        assert r.status_code == 200 and r.json()["created"] == 1, r.text
        assert [s.get("category") for s in await _item_states(session, perm["company_id"]) if s.get("name") == "Batch"] == ["beer"]

    @pytest.mark.asyncio
    async def test_batch_import_refuses_an_ambiguous_category(self, client, session, perm):
        r = await client.post("/items/import/batch", headers=perm["admin_h"], json=self._batch("Red", "batch-red"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["created"] == 0 and any("red_a" in e and "red_b" in e for e in body["errors"]), body
        assert not [s for s in await _item_states(session, perm["company_id"]) if s.get("name") == "Batch"]


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
        ("weight", None), ("mass", None), ("weight_stone", None),
    ])
    def test_weight_header_table(self, header, unit):
        from celerp_inventory.services import weight_unit_from_header
        assert weight_unit_from_header(header, "weight") == unit

    def test_gross_weight_header_carries_its_own_unit(self):
        from celerp_inventory.services import weight_unit_from_header
        assert weight_unit_from_header("Gross weight g", "gross_weight") == "gram"
        assert weight_unit_from_header("Gross weight g", "weight") is None

    def test_mapped_weight_unit_column_wins_over_the_header(self):
        from celerp_inventory.services import source_header_semantics
        assert source_header_semantics({"weight_ct": "weight", "unit": "weight_unit"}, "USD").weight_units == {}
        assert source_header_semantics({"weight_ct": "weight"}, "USD").weight_units == {"weight": "carat"}

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
        ("Price $", "retail_price", "THB", "price_currency_ambiguous"),
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

    # Gemstone acceptance oracle ---------------------------------------------

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
        ref = import_stage.write_stage(_COMPANY_A, "name,sell_by,Price (USD)\nWidget,piece,10\n")
        company = {"id": _COMPANY_A, "currency": "THB", "current_role": "owner", "settings": {}}
        form = {"csv_ref": ref, "map__name": "name", "map__sell_by": "sell_by", "map__Price (USD)": "retail_price"}
        preview = AsyncMock()
        with patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
             patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}])), \
             patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})), \
             patch("ui.api_client.plan_import_rows", new=preview):
            async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                r = await c.post("/inventory/import/mapped", data=form, cookies=_owner_cookies())
        assert r.status_code == 200, r.text
        assert "Price (USD)" in r.text and "different currency" in r.text
        preview.assert_not_awaited()


# ---------------------------------------------------------------------------
# INV-TABULAR-01 - CSV and XLSX are parser variants, not business variants
# ---------------------------------------------------------------------------


class _Upload:
    """Reads like Starlette's UploadFile: ``read(size)`` returns the next chunk."""

    def __init__(self, data: bytes, filename: str):
        self._data = io.BytesIO(data)
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        return self._data.read(size)


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    import io
    import openpyxl
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        worksheet = workbook.create_sheet(title=name)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_PARITY_CSV = "sku,name,quantity,weight,retail_price,note\n007,\"Ruby, oval\",2,1.25,1500,\nB-2,Café,10,0.5,99.5,x\n"
_PARITY_XLSX_ROWS = [
    ["sku", "name", "quantity", "weight", "retail_price", "note"],
    ["007", "Ruby, oval", 2, 1.25, 1500, None],
    ["B-2", "Café", 10, 0.5, 99.5, "x"],
]


async def _read(data: bytes, filename: str, known=(), **fields):
    return await ci.read_tabular_upload({"csv_file": _Upload(data, filename), **fields}, known=known)


class TestTabularParityInvariant:
    @pytest.mark.asyncio
    async def test_csv_and_xlsx_produce_identical_rows(self):
        csv_rows, csv_err = await _read(_PARITY_CSV.encode(), "items.csv")
        xlsx_rows, xlsx_err = await _read(_xlsx({"Items": _PARITY_XLSX_ROWS}), "items.xlsx")
        assert csv_err is None and xlsx_err is None
        assert xlsx_rows == csv_rows
        assert csv_rows[0] == {"sku": "007", "name": "Ruby, oval", "quantity": "2", "weight": "1.25", "retail_price": "1500", "note": ""}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["csv", "xlsx"])
    async def test_row_longer_than_the_header_says_so_in_both_formats(self, fmt):
        from ui.i18n import t
        data = ("sku,name\nA1,Ruby,EXTRA\n".encode() if fmt == "csv"
                else _xlsx({"Items": [["sku", "name"], ["A1", "Ruby", "EXTRA"]]}))
        rows, err = await _read(data, f"items.{fmt}")
        assert rows == [] and err == t("import.err_extra_columns", file=f"items.{fmt}")

    @pytest.mark.asyncio
    async def test_bom_csv_still_reads_its_first_header(self):
        rows, err = await _read(("﻿" + _PARITY_CSV).encode(), "items.csv")
        assert err is None and list(rows[0])[0] == "sku"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["csv", "xlsx"])
    async def test_row_bound_applies_to_both_formats(self, fmt, monkeypatch):
        from celerp.importers import tabular
        monkeypatch.setattr(tabular, "MAX_ROWS", 1)
        data = _PARITY_CSV.encode() if fmt == "csv" else _xlsx({"Items": _PARITY_XLSX_ROWS})
        rows, err = await _read(data, f"items.{fmt}")
        assert rows == [] and "more than the 1 Celerp can import at once" in err

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["csv", "xlsx"])
    async def test_cell_bound_applies_to_both_formats(self, fmt, monkeypatch):
        from celerp.importers import tabular
        monkeypatch.setattr(tabular, "MAX_CELLS", 6)
        data = _PARITY_CSV.encode() if fmt == "csv" else _xlsx({"Items": _PARITY_XLSX_ROWS})
        rows, err = await _read(data, f"items.{fmt}")
        assert rows == [] and "cells, limit 6)" in err

    @pytest.mark.asyncio
    async def test_several_sheets_with_data_require_a_choice(self):
        data = _xlsx({"Rings": [["sku"], ["R1"]], "Notes": [], "Stones": [["sku"], ["S1"]]})
        rows, err = await _read(data, "stock.xlsx")
        assert rows == []
        assert err.sheets == ["Rings", "Stones"]
        html = to_xml(ci.upload_form(template_href="/t", preview_action="/p", error=err))
        assert 'name="sheet"' in html and "Rings" in html and "Stones" in html
        assert 'value="Rings"' not in html.split('name="sheet"')[1].split(">")[0]

    @pytest.mark.asyncio
    async def test_selected_sheet_is_read_deterministically(self):
        data = _xlsx({"Rings": [["sku"], ["R1"]], "Stones": [["sku"], ["S1"]]})
        first = await _read(data, "stock.xlsx", sheet="Stones")
        second = await _read(data, "stock.xlsx", sheet="Stones")
        assert first == second == ([{"sku": "S1"}], None)
        rows, err = await _read(data, "stock.xlsx", sheet="Missing")
        assert rows == [] and err.sheets == ["Rings", "Stones"]

    @pytest.mark.asyncio
    async def test_one_sheet_with_data_is_read_without_asking(self):
        data = _xlsx({"Cover": [], "Items": [["sku"], ["A"]]})
        assert await _read(data, "items.xlsx") == ([{"sku": "A"}], None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("filename", ["items.xls", "items.xlsm", "items.pdf"])
    async def test_unsupported_formats_are_refused_with_a_reason(self, filename):
        rows, err = await _read(b"whatever", filename)
        expected = (t_("import.err_unsupported_type", "en", type=".pdf") if filename.endswith(".pdf")
                    else "files are not supported")
        assert rows == [] and expected in err

    @pytest.mark.asyncio
    async def test_formula_cells_are_refused_with_their_position(self):
        rows, err = await _read(_xlsx({"Items": [["sku", "qty"], ["A", "=1+1"]]}), "items.xlsx")
        assert rows == [] and t_("import.err_formula", "en") in err

    def test_upload_form_accepts_csv_and_xlsx(self):
        html = to_xml(ci.upload_form(template_href="/t", preview_action="/p"))
        assert 'accept=".csv,.xlsx"' in html and "xlsx" in html
        assert 'name="sheet"' not in html

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["/inventory/import/preview", "/lists/import/preview"])
    async def test_importers_stage_the_same_rows_from_csv_and_xlsx(self, route):
        from ui.app import app as ui_app
        company = {"id": _COMPANY_A, "currency": "USD", "current_role": "owner", "settings": {}}
        staged: dict[str, str] = {}
        for fmt, data in (("csv", _PARITY_CSV.encode()), ("xlsx", _xlsx({"Items": _PARITY_XLSX_ROWS}))):
            stash = AsyncMock(return_value="imp_" + "0" * 32)
            with patch("ui.routes.csv_import.stash_import_csv", new=stash), \
                 patch("ui.api_client.get_company", new=AsyncMock(return_value=company)), \
                 patch("ui.api_client.get_price_lists", new=AsyncMock(return_value=[{"name": "Retail"}])), \
                 patch("ui.api_client.get_all_category_schemas", new=AsyncMock(return_value={})):
                async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                    r = await c.post(route, files={"csv_file": (f"items.{fmt}", data)}, cookies=_owner_cookies())
            assert r.status_code == 200, r.text
            stash.assert_awaited_once()
            staged[fmt] = stash.await_args.args[1]
        assert staged["xlsx"] == staged["csv"]
        assert "Ruby, oval" in staged["csv"]

    # A file read by any importer: the header row is found, or chosen, the same
    # way for both formats, and every choice is made on the stored file.

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["csv", "xlsx"])
    async def test_a_title_row_above_the_header_is_skipped_in_both_formats(self, fmt):
        from celerp.importers.tabular import known_headers
        lines = [["Stock list, March"], [], ["sku", "name", "quantity"], ["A1", "Ruby", "2"]]
        data = (b"Stock list March\n\nsku,name,quantity\nA1,Ruby,2\n" if fmt == "csv"
                else _xlsx({"Items": [[c or None for c in line] for line in lines]}))
        rows, err = await _read(data, f"items.{fmt}", known=known_headers(["sku", "name", "quantity"]))
        assert err is None and rows == [{"sku": "A1", "name": "Ruby", "quantity": "2"}]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fmt", ["csv", "xlsx"])
    async def test_an_unclear_header_row_asks_with_the_leading_rows(self, fmt, stage_dir):
        from celerp.importers.tabular import known_headers
        data = (b"Report\nalpha,beta\nA1,Ruby\n" if fmt == "csv"
                else _xlsx({"Items": [["Report"], ["alpha", "beta"], ["A1", "Ruby"]]}))
        stash = AsyncMock()
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A})), \
             patch("ui.routes.csv_import.stash_import_csv", new=stash):
            rows, ref, err = await ci.stage_tabular_upload(
                "tok", {"csv_file": _Upload(data, f"items.{fmt}")}, known=known_headers(["sku", "name"]))
        assert (rows, ref) == ([], "") and err.header_lines[:2] == [["Report"], ["alpha", "beta"]]
        stash.assert_not_awaited()
        html = to_xml(ci.upload_form(template_href="/t", preview_action="/p", error=err))
        assert f'name="source_ref" value="{err.source_ref}"' in html
        assert 'name="header_row"' in html and "alpha | beta" in html and f"items.{fmt}" in html

    @pytest.mark.asyncio
    async def test_the_header_row_is_chosen_on_the_stored_file(self, stage_dir):
        from celerp.importers.tabular import known_headers
        known = known_headers(["sku", "name"])
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A})):
            _rows, _ref, asked = await ci.stage_tabular_upload(
                "tok", {"csv_file": _Upload(b"Report\nalpha,beta\nA1,Ruby\n", "items.csv")}, known=known)
            rows, ref, err = await ci.stage_tabular_upload(
                "tok", {"source_ref": asked.source_ref, "header_row": "1"}, known=known)
            assert err is None and rows == [{"alpha": "A1", "beta": "Ruby"}]
            text, draft, _revision = await ci.load_import_draft("tok", ref)
            assert draft == {"source": {"filename": "items.csv", "sheet": None, "header_row": 1}}
            assert "A1,Ruby" in text
            # The stored file is gone once its rows are staged.
            assert await ci._load_upload_source("tok", asked.source_ref) is None

    @pytest.mark.asyncio
    async def test_the_sheet_is_chosen_on_the_stored_workbook(self, stage_dir):
        data = _xlsx({"Rings": [["sku"], ["R1"]], "Stones": [["sku"], ["S1"]]})
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A})):
            _rows, _ref, asked = await ci.stage_tabular_upload("tok", {"csv_file": _Upload(data, "stock.xlsx")})
            assert asked.sheets == ["Rings", "Stones"] and asked.source_ref
            rows, ref, err = await ci.stage_tabular_upload("tok", {"source_ref": asked.source_ref, "sheet": "Stones"})
        assert err is None and rows == [{"sku": "S1"}] and ref

    @pytest.mark.asyncio
    async def test_a_stored_upload_is_private_to_its_company_and_never_a_table(self, stage_dir):
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A})):
            _rows, _ref, asked = await ci.stage_tabular_upload(
                "tok", {"csv_file": _Upload(b"Report\nalpha,beta\nA1,Ruby\n", "items.csv")},
                known=frozenset({"sku", "name"}))
            assert await ci.load_import_csv("tok", asked.source_ref) is None
            assert await ci.load_import_draft("tok", asked.source_ref) is None
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_B})):
            rows, ref, err = await ci.stage_tabular_upload("tok", {"source_ref": asked.source_ref, "header_row": "1"})
        assert (rows, ref) == ([], "") and err == t_("import.csv_expired")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("key", ["extra_columns", "empty", "no_header"])
    async def test_workbook_errors_name_the_file_and_never_say_csv(self, key):
        sheet = {"extra_columns": [["sku", "name"], ["A1", "Ruby", "EXTRA"]], "empty": [["sku"]],
                 "no_header": [["sku", None, "name"], ["A1", "x", "Ruby"]]}[key]
        rows, err = await _read(_xlsx({"Items": sheet}), "stock.xlsx")
        assert rows == [] and err == t_(f"import.err_{key}", file="stock.xlsx")
        assert "stock.xlsx" in err and "CSV" not in err

    @pytest.mark.asyncio
    async def test_unreadable_workbook_names_the_file(self):
        rows, err = await _read(_xlsx({"Items": [["sku", "qty"], ["A", "=1+1"]]}), "stock.xlsx")
        assert rows == [] and err.startswith(t_("import.err_read_file", file="stock.xlsx", detail=""))
        assert "CSV" not in err

    @pytest.mark.asyncio
    async def test_document_line_upload_refuses_a_value_it_would_lose(self):
        """Document line uploads read through the same table reader: a repeated
        header is refused by column before any line reaches the document."""
        from ui.app import app as ui_app
        add_lines = AsyncMock()
        with patch("ui.api_client.get_company", new=AsyncMock(return_value={"id": _COMPANY_A, "settings": {}})), \
             patch("ui.api_client.patch_doc", new=add_lines), \
             patch("ui.api_client.list_items", new=AsyncMock(return_value={"items": []})), \
             patch("ui.api_client.get_doc", new=AsyncMock(return_value={"entity_id": "doc:1", "line_items": []})):
            async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
                r = await c.post("/docs/doc:1/items/csv", cookies=_owner_cookies(),
                                 files={"file": ("lines.csv", b"sku,quantity,sku\nA1,2,B2\n")})
        assert r.status_code == 400, r.text
        assert "same header" in r.json()["error"]
        add_lines.assert_not_awaited()


def _company(settings: dict) -> dict:
    return {"id": _COMPANY_A, "name": "Acme", "currency": "USD", "settings": settings}



# ---------------------------------------------------------------------------
# Import authority: who may import, what is checked first, which company's staged
# file is read, how a stale review is refused, how a repeat is absorbed, and that
# a confirm step is still required, for every importer.
# ---------------------------------------------------------------------------

_IMPORTERS = {
    "inventory": {"page": "/inventory/import", "confirm": "/inventory/import/confirm",
                  "csv": "name,sell_by\nWidget,piece\n"},
    "contacts": {"page": "/crm/import/contacts", "confirm": "/crm/import/contacts/confirm",
                 "review": "/crm/import/contacts/revalidate",
                 "csv": "name,email\nWidget Buyer,buyer@example.com\n"},
    "documents": {"page": "/docs/import", "confirm": "/docs/import/confirm",
                  "review": "/docs/import/revalidate",
                  "csv": "doc_type,doc_number,contact_name,total\ninvoice,INV-1,Widget Buyer,10\n"},
}

_AUTHORITY_ASPECTS = ["permission_denial", "preflight_rejection", "cross_company_stage",
                      "stale_preview", "exact_retry", "confirm_required"]


class _ImportApi:
    """Stand-in import server. It records every call and answers per scenario."""

    def __init__(self, aspect: str):
        self.aspect = aspect
        self.calls: list[tuple] = []
        self.seen: set = set()

    def _answer(self, keys: tuple, count: int) -> dict:
        from ui.api_client import APIError
        if self.aspect == "permission_denial":
            raise APIError(403, "Forbidden")
        if self.aspect == "preflight_rejection":
            raise APIError(422, "Row 1 has an invalid value")
        if self.aspect == "stale_preview":
            raise APIError(409, "The data changed since it was reviewed")
        fresh = 0 if keys in self.seen else count
        self.seen.add(keys)
        return {"created": fresh, "skipped": count - fresh, "updated": 0, "errors": []}

    async def get_company(self, token):
        return _company({})

    async def import_rows(self, token, rows, *, upsert, idempotency_key, preview_hash, decisions):
        self.calls.append(("import_rows", rows, upsert, idempotency_key, preview_hash, decisions))
        return self._answer((idempotency_key,), len(rows))

    async def plan_import_rows(self, token, rows, *, upsert, idempotency_key, decisions):
        self.calls.append(("plan_import_rows", rows, upsert, idempotency_key, decisions))
        return {"errors": [], "locations_to_create": [], "counts": {"create": len(rows)}, "preview_hash": "reviewed"}

    async def get_price_lists(self, token):
        return []

    async def numbered_ids(self, token, resource, number, doc_type=None):
        self.calls.append(("numbered_ids", resource, number, doc_type))
        return []

    async def batch_import(self, token, path, records, upsert=False):
        stripped = [{k: v for k, v in r.items() if k != "entity_id"} for r in records]
        self.calls.append(("batch_import", path, stripped, upsert))
        return self._answer(tuple(r.get("idempotency_key") for r in records), len(records))

    def writes(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("import_rows", "batch_import")]


def _claimed_created(html: str, confirm_action: str) -> int | None:
    """Rows the result page reports as created. A page that still offers the
    confirm step is a review, not a result, and claims nothing."""
    if f'hx-post="{confirm_action}"' in html:
        return None
    m = re.search(r'class="import-card import-card--success">\s*<div class="import-card-value">(\d+)', html)
    return int(m.group(1)) if m else None


async def _run_import_scenario(importer: str, aspect: str) -> tuple[dict, _ImportApi]:
    from ui.app import app as ui_app
    spec = _IMPORTERS[importer]
    api = _ImportApi(aspect)
    role = "viewer" if aspect == "permission_denial" else "owner"
    cookies = {"celerp_token": make_test_token(role=role)}
    with patch("ui.api_client.get_company", new=api.get_company), \
         patch("ui.api_client.import_rows", new=api.import_rows), \
         patch("ui.api_client.plan_import_rows", new=api.plan_import_rows), \
         patch("ui.api_client.get_price_lists", new=api.get_price_lists), \
         patch("ui.api_client.numbered_ids", new=api.numbered_ids), \
         patch("ui.api_client.batch_import", new=api.batch_import):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            async def submit(path: str, with_hash: bool = True):
                company = _COMPANY_B if aspect == "cross_company_stage" else _COMPANY_A
                data = {"csv_ref": import_stage.write_stage(company, spec["csv"])}
                if with_hash and importer == "inventory":
                    data["preview_hash"] = "reviewed"
                return await c.post(path, data=data, cookies=cookies)

            if aspect == "confirm_required":
                responses = [await submit(spec.get("review", spec["confirm"]), with_hash=False)]
            elif aspect == "exact_retry":
                responses = [await submit(spec["confirm"]), await submit(spec["confirm"])]
            else:
                responses = [await submit(spec["confirm"])]
    outcome = {
        "responses": [(r.status_code, r.headers.get("location"), _claimed_created(r.text, spec["confirm"]),
                       f'hx-post="{spec["confirm"]}"' in r.text) for r in responses],
        "calls": api.calls,
    }
    return outcome, api


class TestImportAuthorityInvariant:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("aspect", _AUTHORITY_ASPECTS)
    @pytest.mark.parametrize("importer", list(_IMPORTERS))
    async def test_import_authority_holds_for_every_importer(self, importer, aspect, stage_dir):
        result, api = await _run_import_scenario(importer, aspect)
        writes = api.writes()
        claimed = [claim for _, _, claim, _ in result["responses"]]
        back_to_review = [review for *_, review in result["responses"]]
        if aspect == "permission_denial":
            assert all(not claim for claim in claimed)
            if importer == "inventory":
                assert writes == [] and result["responses"][0][:2] == (302, "/inventory")
        elif aspect in ("preflight_rejection", "stale_preview"):
            assert all(not claim for claim in claimed)
            if importer == "inventory":
                assert back_to_review == [True]
        elif aspect == "cross_company_stage":
            assert "Widget" not in repr(writes)
            assert all(not claim for claim in claimed)
        elif aspect == "exact_retry":
            assert len(writes) == 2 and writes[0] == writes[1]
            assert claimed == [1, 0]
        elif aspect == "confirm_required":
            assert writes == [] and back_to_review == [True]
