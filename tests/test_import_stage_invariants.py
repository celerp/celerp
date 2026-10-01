# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Staged import files: privacy on disk, lifetime, and retry safety.

A staged upload can hold customers, invoices, costs and tax data, so the
staging helper keeps its directory and files private, removes expired and
orphaned stages (also when the UI app starts), and only discards a stage after
an import finished cleanly. An API error, a transport error, or a result that
reports failed rows keeps the stage so the user can go back and retry.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from celerp.services import import_stage
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token

_REPO = Path(__file__).resolve().parent.parent
_COMPANY_A = "company-a"
_COMPANY_B = "company-b"
_REF_RE = re.compile(r"imp_[0-9a-f]{32}")


@pytest.fixture
def stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path / "import_staging"


@pytest.fixture
def open_umask():
    """Run with a permissive umask so file modes come from the helper, not the process."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _age(path: Path, seconds: float) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past))


def _expired_seconds() -> float:
    return import_stage.MAX_AGE_SECONDS + 3600


def _expire_meta(stage_dir: Path, ref: str) -> None:
    meta = stage_dir / f"{ref}.meta"
    stale = time.time() - _expired_seconds()
    meta.write_text(json.dumps({"company_id": _COMPANY_A, "created_at": stale}))
    _age(meta, _expired_seconds())
    csv_path = stage_dir / f"{ref}.csv"
    if csv_path.exists():
        _age(csv_path, _expired_seconds())


# ---------------------------------------------------------------------------
# Every browser importer that stages a file and finishes with a confirm step
# ---------------------------------------------------------------------------

_BATCH_OK = {"created": 1, "skipped": 0, "updated": 0, "failed": 0, "errors": []}
_BATCH_ROW_ERRORS = {"created": 0, "skipped": 0, "updated": 0, "failed": 1, "errors": ["Row 1: invalid value"]}

# name -> confirm route, staged CSV, a value unique to that CSV, API writer, writer path
_IMPORTERS: dict[str, dict] = {
    "inventory": {
        "confirm": "/inventory/import/confirm",
        "csv": "name,sell_by\nZeta Widget,piece\n",
        "marker": "Zeta Widget",
        "writer": "import_rows",
        "path": "/items/import/rows",
        "extra": {"preview_hash": "c" * 64},
    },
    "contacts": {
        "confirm": "/crm/import/contacts/confirm",
        "csv": "name,email\nZeta Buyer,buyer@example.com\n",
        "marker": "Zeta Buyer",
        "writer": "batch_import",
        "path": "/crm/contacts/import/batch",
    },
    "documents": {
        "confirm": "/docs/import/confirm",
        "csv": "doc_type,doc_number\ninvoice,INV-Z9001\n",
        "marker": "INV-Z9001",
        "writer": "batch_import",
        "path": "/docs/import/batch",
    },
    "lists": {
        "confirm": "/lists/import/confirm",
        "csv": "ref_id,status\nL-Z9001,draft\n",
        "marker": "L-Z9001",
        "writer": "batch_import",
        "path": "/lists/import/batch",
    },
    "subscriptions": {
        "confirm": "/subscriptions/import/confirm",
        "csv": "name,frequency,start_date\nZeta Retainer,monthly,2026-01-01\n",
        "marker": "Zeta Retainer",
        "writer": "create_subscription",
        "path": "/docs",
    },
    "chart": {
        "confirm": "/accounting/import/chart/confirm",
        "csv": "code,name,account_type\n1901,Zeta Holdings Cash,asset\n",
        "marker": "Zeta Holdings Cash",
        "writer": "batch_import",
        "path": "/accounting/accounts/import/batch",
    },
    "locations": {
        "confirm": "/settings/import/locations/confirm",
        "csv": "name,type\nZeta Vault,store\n",
        "marker": "Zeta Vault",
        "writer": "batch_import",
        "path": "/companies/me/locations/import/batch",
    },
    "taxes": {
        "confirm": "/settings/import/taxes/confirm",
        "csv": "name,rate\nZeta VAT,7\n",
        "marker": "Zeta VAT",
        "writer": "batch_import",
        "path": "/companies/me/taxes/import/batch",
    },
    "terms": {
        "confirm": "/settings/import/payment-terms/confirm",
        "csv": "name,days\nZeta Net 45,45\n",
        "marker": "Zeta Net 45",
        "writer": "batch_import",
        "path": "/companies/me/payment-terms/import/batch",
    },
}

_ALL = sorted(_IMPORTERS)


def _route_path(decorator: ast.expr) -> str | None:
    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "post" and decorator.args
            and isinstance(decorator.args[0], ast.Constant) and isinstance(decorator.args[0].value, str)):
        return decorator.args[0].value
    return None


def _calls(fn: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            names.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return names


def _staging_confirm_routes() -> set[str]:
    """Every UI POST route that discards a stage, or confirms an import read from one."""
    found = set()
    for path in sorted((_REPO / "ui" / "routes").glob("*.py")):
        if path.name == "csv_import.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            routes = [p for p in map(_route_path, node.decorator_list) if p]
            if not routes:
                continue
            called = _calls(node)
            for route in routes:
                if "discard_import_csv" in called or (
                    route.endswith("/confirm") and "resolve_import_csv" in called
                ):
                    found.add(route)
    return found


def test_importer_table_covers_every_staging_confirm_route():
    """A new importer that stages a file must be added to the retry-safety matrix."""
    assert _staging_confirm_routes() == {spec["confirm"] for spec in _IMPORTERS.values()}


def _company(company_id: str) -> dict:
    return {"id": company_id, "name": "Acme", "currency": "USD", "current_role": "owner", "settings": {}}


def _cookies() -> dict:
    return {"celerp_token": make_test_token(role="owner")}


@contextmanager
def _patched_api(company_id: str = _COMPANY_A, **writers):
    """Stub the UI's API client: the caller's company plus the given writer functions."""
    targets = {
        "get_company": AsyncMock(return_value=_company(company_id)),
        "numbered_ids": AsyncMock(return_value=[]),
        "get_price_lists": AsyncMock(return_value=[{"name": "Retail"}]),
        "get_all_category_schemas": AsyncMock(return_value={}),
        **writers,
    }
    patches = [patch(f"ui.api_client.{name}", new=mock) for name, mock in targets.items()]
    for p in patches:
        p.start()
    try:
        yield targets
    finally:
        for p in reversed(patches):
            p.stop()


_SUBSCRIPTIONS_APP = None


def _app_for(path: str):
    """The UI app, or for subscription import routes an app carrying just those routes,
    since the main UI app does not mount them."""
    global _SUBSCRIPTIONS_APP
    from ui.app import app as ui_app
    if not path.startswith("/subscriptions/import"):
        return ui_app
    if any(getattr(r, "path", None) == path for r in ui_app.routes):
        return ui_app
    if _SUBSCRIPTIONS_APP is None:
        from fasthtml.common import FastHTML
        from ui.routes import subscriptions_import
        _SUBSCRIPTIONS_APP = FastHTML()
        subscriptions_import.setup_routes(_SUBSCRIPTIONS_APP)
    return _SUBSCRIPTIONS_APP


async def _post(path: str, data: dict | None = None, files: dict | None = None) -> httpx.Response:
    transport = ASGITransport(app=_app_for(path), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://ui") as c:
        return await c.post(path, data=data, files=files, cookies=_cookies())


async def _confirm(name: str, writer, *, csv_text: str | None = None, company_id: str = _COMPANY_A):
    """Stage the importer's CSV for company A, then confirm it as ``company_id``."""
    spec = _IMPORTERS[name]
    ref = import_stage.write_stage(_COMPANY_A, csv_text or spec["csv"])
    with _patched_api(company_id, **{spec["writer"]: writer}):
        r = await _post(spec["confirm"], data={"csv_ref": ref, **spec.get("extra", {})})
    return ref, r


def _api_error(status: int, detail: str):
    from ui.api_client import APIError
    return APIError(status, detail)


def _clean_result(name: str):
    if name == "subscriptions":
        return AsyncMock(return_value={"id": "doc:sub-1"})
    return AsyncMock(return_value=dict(_BATCH_OK))


async def _assert_api_error_keeps_stage(name: str, stage_dir: Path) -> None:
    writer = AsyncMock(side_effect=_api_error(500, "server error"))
    ref, r = await _confirm(name, writer)
    assert r.status_code == 200, r.text
    assert writer.await_count >= 1
    assert import_stage.read_stage(_COMPANY_A, ref) == _IMPORTERS[name]["csv"]


# ---------------------------------------------------------------------------
# Terminal results decide whether the stage may be discarded
# ---------------------------------------------------------------------------


class TestStageRetrySafety:
    @pytest.mark.asyncio
    async def test_contacts_api_error_keeps_stage(self, stage_dir):
        await _assert_api_error_keeps_stage("contacts", stage_dir)

    @pytest.mark.asyncio
    async def test_documents_api_error_keeps_stage(self, stage_dir):
        await _assert_api_error_keeps_stage("documents", stage_dir)

    @pytest.mark.asyncio
    async def test_lists_api_error_keeps_stage(self, stage_dir):
        await _assert_api_error_keeps_stage("lists", stage_dir)

    @pytest.mark.asyncio
    async def test_inventory_api_error_keeps_stage(self, stage_dir):
        await _assert_api_error_keeps_stage("inventory", stage_dir)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _ALL)
    async def test_every_importer_api_error_keeps_stage(self, name, stage_dir):
        await _assert_api_error_keeps_stage(name, stage_dir)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("error", [httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError])
    @pytest.mark.parametrize("name", _ALL)
    async def test_every_importer_transport_error_keeps_stage(self, name, error, stage_dir, monkeypatch):
        """The request may have reached the server before the connection failed, so the
        outcome is unknown and the staged source must survive for a retry."""
        spec = _IMPORTERS[name]
        writes: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path.endswith("/numbered"):
                return httpx.Response(200, json={"ids": []})
            if request.method == "POST" and request.url.path == spec["path"]:
                writes.append(request.url.path)
                raise error("connection lost", request=request)
            return httpx.Response(404, json={"detail": "not stubbed"})

        mock_transport = httpx.MockTransport(handler)
        monkeypatch.setattr("ui.api_client._get_transport", lambda: mock_transport)
        monkeypatch.setattr("ui.api_client._get_bulk_transport", lambda: mock_transport)

        ref = import_stage.write_stage(_COMPANY_A, spec["csv"])
        with _patched_api(_COMPANY_A):
            r = await _post(spec["confirm"], data={"csv_ref": ref, **spec.get("extra", {})})
        assert r.status_code == 200, r.text
        assert writes, "the import never reached the API writer"
        assert import_stage.read_stage(_COMPANY_A, ref) == spec["csv"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _ALL)
    async def test_row_error_result_keeps_stage_for_retry(self, name, stage_dir):
        spec = _IMPORTERS[name]
        csv_text = spec["csv"]
        if name == "subscriptions":
            csv_text = spec["csv"] + "Zeta Second,monthly,2026-02-01\n"
            writer = AsyncMock(side_effect=[{"id": "doc:sub-1"}, _api_error(422, "invalid frequency")])
        else:
            writer = AsyncMock(return_value=dict(_BATCH_ROW_ERRORS))
        ref, r = await _confirm(name, writer, csv_text=csv_text)
        assert r.status_code == 200, r.text
        assert writer.await_count >= 1
        assert import_stage.read_stage(_COMPANY_A, ref) == csv_text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _ALL)
    async def test_clean_terminal_success_deletes_stage(self, name, stage_dir):
        writer = _clean_result(name)
        ref, r = await _confirm(name, writer)
        assert r.status_code == 200, r.text
        assert writer.await_count >= 1
        assert import_stage.read_stage(_COMPANY_A, ref) is None
        assert not (stage_dir / f"{ref}.csv").exists()
        assert not (stage_dir / f"{ref}.meta").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _ALL)
    async def test_cross_company_ref_still_fails_closed(self, name, stage_dir):
        """Company B presenting company A's exact reference reads nothing, writes nothing
        from it, and cannot remove it."""
        spec = _IMPORTERS[name]
        writer = _clean_result(name)
        ref, r = await _confirm(name, writer, company_id=_COMPANY_B)
        assert r.status_code in (200, 302, 303), r.text
        for call in writer.await_args_list:
            sent = json.dumps([a for a in call.args[1:]] + list(call.kwargs.values()), default=str)
            assert spec["marker"] not in sent
            if spec["writer"] == "batch_import":
                assert call.args[2] == []
        if spec["writer"] != "batch_import":
            writer.assert_not_awaited()
        assert spec["marker"] not in r.text
        assert import_stage.read_stage(_COMPANY_A, ref) == spec["csv"]


# ---------------------------------------------------------------------------
# A valid stage survives the whole browser flow for its own company
# ---------------------------------------------------------------------------


def _last_ref(html: str) -> str:
    refs = _REF_RE.findall(html)
    assert refs, html[:2000]
    return refs[-1]


class TestStageSurvivesFlow:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _ALL)
    async def test_same_company_stage_survives_mapping_revalidate_confirm(self, name, stage_dir):
        spec = _IMPORTERS[name]
        base = spec["confirm"].rsplit("/", 1)[0]
        cols = spec["csv"].splitlines()[0].split(",")
        writer = _clean_result(name)
        preview = AsyncMock(return_value={"errors": [], "locations_to_create": [], "preview_hash": "e" * 64})
        with _patched_api(_COMPANY_A, **{spec["writer"]: writer, "preview_import_rows": preview}):
            r = await _post(f"{base}/preview", files={"csv_file": ("data.csv", spec["csv"].encode())})
            assert r.status_code == 200, r.text
            ref = _last_ref(r.text)

            r = await _post(f"{base}/mapped", data={"csv_ref": ref, **{f"map__{c}": c for c in cols}})
            assert r.status_code == 200, r.text
            ref = _last_ref(r.text)

            r = await _post(f"{base}/revalidate", data={"csv_ref": ref})
            assert r.status_code == 200, r.text
            ref = _last_ref(r.text)
            assert spec["marker"] in import_stage.read_stage(_COMPANY_A, ref)

            confirm = {"csv_ref": ref}
            if name == "inventory":
                r = await _post(f"{base}/review", data={"csv_ref": ref})
                assert r.status_code == 200, r.text
                ref = _last_ref(r.text)
                confirm = {"csv_ref": ref, "preview_hash": "e" * 64}

            r = await _post(spec["confirm"], data=confirm)
        assert r.status_code == 200, r.text
        assert writer.await_count >= 1
        sent = json.dumps([list(c.args[1:]) + list(c.kwargs.values()) for c in writer.await_args_list], default=str)
        assert spec["marker"] in sent


# ---------------------------------------------------------------------------
# Filesystem privacy
# ---------------------------------------------------------------------------


class TestStagePrivacy:
    def test_stage_directory_is_private(self, stage_dir, open_umask):
        import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        assert stage_dir.is_dir()
        assert _mode(stage_dir) == 0o700

    def test_stage_files_are_private(self, stage_dir, open_umask):
        ref = import_stage.write_stage(_COMPANY_A, "sku\nA\n")
        csv_path, meta_path = stage_dir / f"{ref}.csv", stage_dir / f"{ref}.meta"
        assert _mode(csv_path) == 0o600
        assert _mode(meta_path) == 0o600
        # Writes are complete stage pairs; no temporary file is left behind.
        assert sorted(p.name for p in stage_dir.iterdir()) == sorted([csv_path.name, meta_path.name])
        assert import_stage.read_stage(_COMPANY_A, ref) == "sku\nA\n"

    def test_preexisting_stage_dir_and_files_are_made_private(self, stage_dir, open_umask):
        stage_dir.mkdir(parents=True)
        os.chmod(stage_dir, 0o777)
        old_ref = "imp_" + "a" * 32
        old_csv, old_meta = stage_dir / f"{old_ref}.csv", stage_dir / f"{old_ref}.meta"
        old_csv.write_text("sku\nOLD\n", encoding="utf-8")
        old_meta.write_text(json.dumps({"company_id": _COMPANY_A, "created_at": time.time()}), encoding="utf-8")
        os.chmod(old_csv, 0o666)
        os.chmod(old_meta, 0o644)

        ref = import_stage.write_stage(_COMPANY_A, "sku\nNEW\n")

        assert _mode(stage_dir) == 0o700
        assert _mode(stage_dir / f"{ref}.csv") == 0o600
        assert _mode(stage_dir / f"{ref}.meta") == 0o600
        assert _mode(old_csv) == 0o600
        assert _mode(old_meta) == 0o600
        # Tightening modes never costs a user a recent stage.
        assert import_stage.read_stage(_COMPANY_A, old_ref) == "sku\nOLD\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", _ALL)
    async def test_stage_errors_do_not_expose_paths(self, name, stage_dir, tmp_path):
        """When the stage cannot be written the upload fails with a readable message
        that does not reveal where staged files live on the server."""
        stage_dir.parent.mkdir(parents=True, exist_ok=True)
        stage_dir.write_text("not a directory", encoding="utf-8")
        spec = _IMPORTERS[name]
        base = spec["confirm"].rsplit("/", 1)[0]
        with _patched_api(_COMPANY_A):
            r = await _post(f"{base}/preview", files={"csv_file": ("data.csv", spec["csv"].encode())})
        assert r.status_code == 200, r.text[:2000]
        assert str(tmp_path) not in r.text
        assert "import_staging" not in r.text
        assert _REF_RE.search(r.text) is None


# ---------------------------------------------------------------------------
# Orphans and startup cleanup
# ---------------------------------------------------------------------------


class TestStageCleanup:
    def test_orphan_csv_is_cleaned_after_ttl(self, stage_dir):
        old = import_stage.write_stage(_COMPANY_A, "old")
        recent = import_stage.write_stage(_COMPANY_A, "recent")
        (stage_dir / f"{old}.meta").unlink()
        (stage_dir / f"{recent}.meta").unlink()
        _age(stage_dir / f"{old}.csv", _expired_seconds())

        assert import_stage.cleanup_expired() == 1
        assert not (stage_dir / f"{old}.csv").exists()
        assert (stage_dir / f"{recent}.csv").exists()

    def test_orphan_meta_is_cleaned_after_ttl(self, stage_dir):
        old = import_stage.write_stage(_COMPANY_A, "old")
        recent = import_stage.write_stage(_COMPANY_A, "recent")
        (stage_dir / f"{old}.csv").unlink()
        (stage_dir / f"{recent}.csv").unlink()
        _expire_meta(stage_dir, old)

        assert import_stage.cleanup_expired() == 1
        assert not (stage_dir / f"{old}.meta").exists()
        assert (stage_dir / f"{recent}.meta").exists()

    @pytest.mark.asyncio
    async def test_startup_cleanup_removes_expired_stages(self, stage_dir):
        from ui.app import app as ui_app
        expired = import_stage.write_stage(_COMPANY_A, "expired")
        orphan = import_stage.write_stage(_COMPANY_A, "orphan")
        fresh = import_stage.write_stage(_COMPANY_A, "fresh")
        _expire_meta(stage_dir, expired)
        (stage_dir / f"{orphan}.meta").unlink()
        _age(stage_dir / f"{orphan}.csv", _expired_seconds())

        async with ui_app.router.lifespan_context(ui_app):
            remaining = {p.name for p in stage_dir.iterdir()}

        assert remaining == {f"{fresh}.csv", f"{fresh}.meta"}
        assert import_stage.read_stage(_COMPANY_A, fresh) == "fresh"
