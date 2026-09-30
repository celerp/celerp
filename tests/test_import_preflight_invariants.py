# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""One semantic preflight guards every item import write.

Preview and commit run the same preflight over the same rows, so a row that
preview rejects is rejected by commit with the same field and code, and a row
that preview accepts reaches the writer unchanged. Rows posted without a
preview hash still pass through that preflight. A bound commit refuses when the
meaning of the rows changed since preview, even if the rows themselves did not.
Missing locations are created only for a clean import, once, even when two
imports name the same new location at the same time.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _rows_preview(client, h, rows, *, upsert=False, key="op-1") -> dict:
    r = await client.post(
        "/items/import/rows/preview",
        json={"rows": rows, "upsert": upsert, "idempotency_key": key}, headers=h,
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _rows_commit(client, h, rows, *, upsert=False, key="op-1", preview_hash=None):
    return await client.post("/items/import/rows", json={
        "rows": rows, "upsert": upsert, "idempotency_key": key, "preview_hash": preview_hash,
    }, headers=h)


async def _seed_items(client, h, rows, key):
    r = await _rows_commit(client, h, rows, key=key)
    assert r.status_code == 200 and not r.json()["errors"], r.text


def _fc(errors: list[dict]) -> list[tuple]:
    """Field and code of each error, order-free."""
    return sorted((e["field"], e.get("code")) for e in errors)


def _rfc(errors: list[dict]) -> list[tuple]:
    return sorted((e["row"], e["field"], e.get("code")) for e in errors)


def _rejected_errors(r) -> list[dict]:
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict), detail
    return detail["errors"]


async def _item_count(session, company_id: str) -> int:
    """Real items only: the first real import clears the sample items."""
    from celerp.models.projections import Projection
    return (await session.execute(
        select(func.count()).select_from(Projection).where(
            Projection.company_id == uuid.UUID(str(company_id)), Projection.entity_type == "item",
            Projection.entity_id.not_like("item:demo-%"),
        )
    )).scalar_one()


async def _location_names(session, company_id: str) -> list[str]:
    from celerp.models.company import Location
    return sorted((await session.execute(
        select(Location.name).where(Location.company_id == uuid.UUID(str(company_id)))
    )).scalars().all())


async def _item_state_by_sku(session, company_id: str, sku: str) -> dict | None:
    from celerp.models.projections import Projection
    rows = (await session.execute(
        select(Projection).where(
            Projection.company_id == uuid.UUID(str(company_id)), Projection.entity_type == "item",
        )
    )).scalars().all()
    for row in rows:
        await session.refresh(row)
        if (row.state or {}).get("sku") == sku:
            return dict(row.state)
    return None


async def _set_company_settings(session, company_id, **values) -> None:
    from celerp.services.company_lock import locked_company
    company = await locked_company(session, uuid.UUID(str(company_id)))
    company.settings = {**(company.settings or {}), **values}
    await session.commit()


async def _company_settings(session, company_id) -> dict:
    from celerp.models.company import Company
    company = await session.get(Company, uuid.UUID(str(company_id)))
    await session.refresh(company)
    return dict(company.settings or {})


def _writer_spy(monkeypatch) -> AsyncMock:
    """Watch the low-level writer every import ends in."""
    import celerp_inventory.services as svc
    spy = AsyncMock(wraps=svc.commit_import_batch)
    monkeypatch.setattr(svc, "commit_import_batch", spy)
    return spy


def _preflight_spy(monkeypatch) -> AsyncMock:
    """Watch the canonical semantic preflight. Where it does not exist, the spy is
    never awaited and the caller's call-count assertion fails."""
    import celerp_inventory.services as svc
    real = getattr(svc, "preflight_import_rows", None)
    spy = AsyncMock(wraps=real) if real is not None else AsyncMock()
    monkeypatch.setattr(svc, "preflight_import_rows", spy, raising=False)
    return spy


def _import_items_spy(monkeypatch) -> AsyncMock:
    import celerp_inventory.routes as routes
    spy = AsyncMock(wraps=routes.import_items)
    monkeypatch.setattr(routes, "import_items", spy)
    return spy


def _user_id(h: dict) -> str:
    from celerp.services.auth import decode_access_token
    return decode_access_token(h["Authorization"].split()[1])["sub"]


def _csv_text(rows: list[dict]) -> str:
    cols: list[str] = ["name"]
    for row in rows:
        cols += [k for k in row if k not in cols]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in cols})
    return buf.getvalue()


@pytest.fixture
async def perm(client, session):
    from celerp.services.auth import decode_access_token
    from test_helpers import perm_setup
    s = await perm_setup(client, session)
    claims = decode_access_token(s["admin_h"]["Authorization"].split()[1])
    s["company_id"], s["admin_user_id"] = claims["company_id"], claims["sub"]
    return s


@pytest.fixture
def write_upload():
    """Seed an owned transient upload, as the upload endpoint would."""
    from celerp.ai.files import upload_dir

    def _write(company_id: str, user_id: str, text: str, *, filename: str = "items.csv") -> str:
        file_id = f"ai_up_{uuid.uuid4().hex}"
        data = text.encode()
        (upload_dir() / f"{file_id}.bin").write_bytes(data)
        (upload_dir() / f"{file_id}.meta").write_text(json.dumps({
            "filename": filename, "content_type": "text/csv", "size": len(data),
            "company_id": company_id, "user_id": user_id,
        }))
        return file_id
    return _write


async def _create_item(client, h, location_id, *, sku, name, quantity=1) -> str:
    r = await client.post("/items", json={
        "sku": sku, "name": name, "quantity": quantity, "location_id": location_id,
        "sell_by": "piece", "status": "available",
    }, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _rename_sku(client, h, entity_id, old, new) -> None:
    r = await client.patch(
        f"/items/{entity_id}", json={"fields_changed": {"sku": {"old": old, "new": new}}}, headers=h,
    )
    assert r.status_code == 200, r.text


async def _plan(session, company_id, rows, *, upsert, key="op-1", role="owner"):
    from celerp_inventory.services import preflight_import_rows
    settings = await _company_settings(session, company_id)
    return await preflight_import_rows(
        session, uuid.UUID(str(company_id)), role, settings, rows, upsert=upsert, operation_key=key,
    )


# ---------------------------------------------------------------------------
# Preview and commit reject the same rows with the same field and code
# ---------------------------------------------------------------------------


async def _assert_preview_and_commits_reject(client, session, perm, monkeypatch, h, rows, expected, *, upsert=False):
    """Preview, a bound commit, and a direct commit all reject with ``expected``."""
    preview = await _rows_preview(client, h, rows, upsert=upsert, key="op-rej")
    assert set(expected) <= set(_fc(preview["errors"])), preview["errors"]
    writer = _writer_spy(monkeypatch)
    before = await _item_count(session, perm["company_id"])

    bound = await _rows_commit(client, h, rows, upsert=upsert, key="op-rej", preview_hash=preview["preview_hash"])
    assert _rfc(_rejected_errors(bound)) == _rfc(preview["errors"])

    direct = await _rows_commit(client, h, rows, upsert=upsert, key="op-rej-direct")
    assert _rfc(_rejected_errors(direct)) == _rfc(preview["errors"])

    writer.assert_not_awaited()
    assert await _item_count(session, perm["company_id"]) == before


async def test_preview_and_commit_reject_negative_quantity_identically(client, session, perm, monkeypatch):
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "-1"}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["admin_h"], rows, [("quantity", "negative_value")])


@pytest.mark.parametrize("field", ["quantity", "retail_price"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
async def test_preview_and_commit_reject_nonfinite_numbers_identically(client, session, perm, monkeypatch, field, value):
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1", field: value}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["admin_h"], rows, [(field, "not_finite")])


async def test_preview_and_commit_reject_invalid_sku_identically(client, session, perm, monkeypatch):
    rows = [{"name": "Widget", "sku": "A,B", "sell_by": "piece", "quantity": "1"}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["admin_h"], rows, [("sku", "invalid_sku")])


async def test_preview_and_commit_reject_invalid_barcode_identically(client, session, perm, monkeypatch):
    rows = [{"name": "Widget", "barcode": "12AB", "sell_by": "piece", "quantity": "1"}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["admin_h"], rows, [("barcode", "invalid_barcode")])


async def test_preview_rejects_fractional_quantity_for_zero_decimal_unit(client, session, perm, monkeypatch):
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1.5"}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["admin_h"], rows, [("quantity", "quantity_precision")])


async def test_preview_reports_price_permission_before_commit(client, session, perm, monkeypatch):
    from test_helpers import grant_permission
    await grant_permission(client, perm["admin_h"], "set_inventory_prices", "admin")
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1", "retail_price": "10"}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["manager_h"], rows, [("retail_price", "permission_denied")])


async def test_preview_reports_amount_permission_before_commit(client, session, perm, monkeypatch):
    from test_helpers import grant_permission
    await _seed_items(client, perm["admin_h"], [{"name": "Amount", "sku": "AM-1", "sell_by": "piece", "quantity": "1"}], key="seed-am")
    await grant_permission(client, perm["admin_h"], "edit_inventory_amounts", "admin")
    rows = [{"name": "Amount", "sku": "AM-1", "sell_by": "piece", "quantity": "5"}]
    await _assert_preview_and_commits_reject(
        client, session, perm, monkeypatch, perm["manager_h"], rows, [("quantity", "permission_denied")], upsert=True)
    state = await _item_state_by_sku(session, perm["company_id"], "AM-1")
    assert float(state.get("quantity") or 0) == 1


# ---------------------------------------------------------------------------
# Direct rows (no preview hash) still go through the semantic preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["quantity", "pieces", "weight", "retail_price"])
async def test_bad_numeric_cannot_silently_become_zero_or_none(client, session, perm, monkeypatch, field):
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1", field: "not-a-number"}]
    if field == "weight":
        rows[0]["weight_unit"] = "gram"
    writer = _writer_spy(monkeypatch)
    before = await _item_count(session, perm["company_id"])
    r = await _rows_commit(client, perm["admin_h"], rows, key=f"op-bad-{field}")
    assert (field, "invalid_value") in _fc(_rejected_errors(r))
    writer.assert_not_awaited()
    assert await _item_count(session, perm["company_id"]) == before


@pytest.mark.parametrize("field,sell_by", [("qty", "piece"), ("weight_ct", "carat")])
@pytest.mark.parametrize("value,code", [("nan", "not_finite"), ("inf", "not_finite"), ("abc", "invalid_value")])
async def test_non_finite_and_malformed_amounts_are_rejected_before_anything_is_written(
        client, session, perm, monkeypatch, field, sell_by, value, code):
    # qty and weight_ct are row keys the writer reads as the stock amount and the
    # source weight, so they are checked like quantity and weight.
    rows = [{"name": "Widget", "sell_by": sell_by, field: value, "location_name": f"Amount {field} {value}"}]
    await _assert_preview_and_commits_reject(client, session, perm, monkeypatch, perm["admin_h"], rows, [(field, code)])
    assert f"Amount {field} {value}" not in await _location_names(session, perm["company_id"])


async def test_writer_failure_is_reported_without_internal_detail(client, session, perm, monkeypatch):
    import celerp_inventory.services as svc

    async def _fail(*_args, **_kwargs):
        raise RuntimeError("[SQL: INSERT INTO ledger (secret_column) VALUES ($1)] [parameters: ('internal',)]")

    monkeypatch.setattr(svc, "emit_event", _fail)
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1"}]
    r = await _rows_commit(client, perm["admin_h"], rows, key="op-writer-fail")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0 and body["skipped"] == 1, body
    assert body["errors"], body
    text = json.dumps(body["errors"])
    for leaked in ("SQL", "INSERT", "ledger", "parameters", "secret_column", "RuntimeError"):
        assert leaked not in text, body["errors"]


@pytest.mark.parametrize("row", [
    {"name": "", "sell_by": "piece", "quantity": "1"},
    {"name": "   ", "sell_by": "piece", "quantity": "1"},
    {"sell_by": "piece", "quantity": "1"},
], ids=["empty", "whitespace", "absent"])
async def test_blank_name_cannot_bypass_preflight_through_direct_rows(client, session, perm, monkeypatch, row):
    writer = _writer_spy(monkeypatch)
    before = await _item_count(session, perm["company_id"])
    r = await _rows_commit(client, perm["admin_h"], [row], key="op-blank")
    assert ("name", "required") in _fc(_rejected_errors(r))
    writer.assert_not_awaited()
    assert await _item_count(session, perm["company_id"]) == before


async def test_direct_rows_without_preview_hash_still_runs_semantic_preflight(client, session, perm, monkeypatch):
    rows = [
        {"name": "Good", "sell_by": "piece", "quantity": "1"},
        {"name": "No unit", "quantity": "1"},
        {"name": "Bad unit", "sell_by": "furlong", "quantity": "1"},
    ]
    preview = await _rows_preview(client, perm["admin_h"], rows, key="op-direct")
    preflight = _preflight_spy(monkeypatch)
    writer = _writer_spy(monkeypatch)
    before = await _item_count(session, perm["company_id"])
    r = await _rows_commit(client, perm["admin_h"], rows, key="op-direct")
    assert _rfc(_rejected_errors(r)) == _rfc(preview["errors"])
    assert preflight.await_count >= 1
    assert preflight.await_args.args[4] == rows
    writer.assert_not_awaited()
    assert await _item_count(session, perm["company_id"]) == before


async def test_direct_rows_without_preview_hash_still_imports_valid_rows(client, session, perm):
    rows = [
        {"name": "First", "sell_by": "piece", "quantity": "1"},
        {"name": "Second", "sell_by": "piece", "quantity": "2"},
    ]
    before = await _item_count(session, perm["company_id"])
    r = await _rows_commit(client, perm["admin_h"], rows, key="op-valid-direct")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert body["created"] == 2
    assert await _item_count(session, perm["company_id"]) == before + 2


async def test_direct_rows_rejection_is_structured_for_known_callers(client, session, perm, monkeypatch):
    import celerp.main
    import ui.api_client as api
    import ui.config
    from ui.api_client import APIError

    rows = [{"name": "", "sell_by": "piece", "quantity": "1"}]
    errors = _rejected_errors(await _rows_commit(client, perm["admin_h"], rows, key="op-struct"))
    assert errors
    for e in errors:
        assert isinstance(e["row"], int)
        assert isinstance(e["field"], str) and e["field"]
        assert isinstance(e["code"], str) and e["code"]
        assert isinstance(e["message"], str) and e["message"]

    # The browser client's import call surfaces the same structured errors.
    monkeypatch.setattr(ui.config, "API_BASE", "http://api")
    monkeypatch.setattr(api, "_get_bulk_transport", lambda: ASGITransport(app=celerp.main.app))
    token = perm["admin_h"]["Authorization"].split()[1]
    with pytest.raises(APIError) as caught:
        await api.import_rows(token, rows, upsert=False, idempotency_key="op-struct-ui")
    assert caught.value.status == 422
    payload = caught.value.data if isinstance(caught.value.data, dict) else caught.value.detail
    assert isinstance(payload, dict), payload
    assert _rfc(payload["errors"]) == _rfc(errors)


# ---------------------------------------------------------------------------
# Missing locations: created only for a clean import, and only once
# ---------------------------------------------------------------------------


async def test_invalid_row_with_missing_location_creates_no_location(client, session, perm, monkeypatch):
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "-1", "location_name": "Ghost"}]
    writer = _writer_spy(monkeypatch)

    direct = await _rows_commit(client, perm["admin_h"], rows, key="op-ghost")
    assert _rejected_errors(direct)
    assert "Ghost" not in await _location_names(session, perm["company_id"])

    preview = await _rows_preview(client, perm["admin_h"], rows, key="op-ghost-bound")
    bound = await _rows_commit(client, perm["admin_h"], rows, key="op-ghost-bound", preview_hash=preview["preview_hash"])
    assert _rejected_errors(bound)
    assert "Ghost" not in await _location_names(session, perm["company_id"])
    writer.assert_not_awaited()


def _factory(engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


async def _seed_race_company(factory) -> tuple[uuid.UUID, uuid.UUID]:
    from celerp.models.company import Company, Location, User
    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="RaceCo", slug=f"race-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"owner-{user_id.hex[:8]}@example.test", name="Owner",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(Location(id=uuid.uuid4(), company_id=company_id, name="Main",
                       type="warehouse", is_default=True))
        await s.commit()
    return company_id, user_id


async def _until_blocked_or_done(engine, task: asyncio.Task) -> None:
    from sqlalchemy import text
    for _ in range(400):
        if task.done():
            return
        async with engine.connect() as conn:
            waiting = (await conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            ))).scalar_one()
        if waiting:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("the other import neither blocked nor finished")


async def test_concurrent_same_missing_location_does_not_duplicate_or_abort(committed_engine, monkeypatch):
    import celerp_inventory.services as svc
    from celerp.models.company import Location

    factory = _factory(committed_engine)
    company_id, user_id = await _seed_race_company(factory)

    paused, release = asyncio.Event(), asyncio.Event()
    real_writer = svc.commit_import_batch

    async def _held(*args, **kwargs):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real_writer(*args, **kwargs)

    monkeypatch.setattr(svc, "commit_import_batch", _held)

    def _run(session, name, key):
        rows = [{"name": name, "sell_by": "piece", "quantity": "1", "location_name": "Annex"}]
        return svc.import_items(session, company_id, user_id, "owner", {}, rows,
                                upsert=False, filename=None, idempotency_key=key)

    async with factory() as s_first, factory() as s_other:
        first = asyncio.create_task(_run(s_first, "First", "race-first"))
        await asyncio.wait_for(paused.wait(), timeout=10)
        other = asyncio.create_task(_run(s_other, "Other", "race-other"))
        await _until_blocked_or_done(committed_engine, other)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(first, other, return_exceptions=True), timeout=30)

    failures = [o for o in outcomes if isinstance(o, BaseException)]
    assert not failures, failures
    for result in outcomes:
        assert result.errors == []
        assert result.created == 1

    async with factory() as s:
        annex = (await s.execute(select(func.count()).select_from(Location).where(
            Location.company_id == company_id, Location.name == "Annex",
        ))).scalar_one()
    assert annex == 1


# ---------------------------------------------------------------------------
# The semantic fingerprint binds what the rows mean, not only their text
# ---------------------------------------------------------------------------


async def test_semantic_fingerprint_changes_when_upsert_target_changes(client, session, perm):
    rows = [{"name": "Renamed", "sku": "UT-X", "sell_by": "piece"}]
    first_id = await _create_item(client, perm["admin_h"], perm["location_id"], sku="UT-X", name="First")
    before = await _plan(session, perm["company_id"], rows, upsert=True)
    await _rename_sku(client, perm["admin_h"], first_id, "UT-X", "UT-Y")
    await _create_item(client, perm["admin_h"], perm["location_id"], sku="UT-X", name="Second")
    after = await _plan(session, perm["company_id"], rows, upsert=True)
    assert before.errors == [] and after.errors == []
    assert before.semantic_fingerprint != after.semantic_fingerprint


async def test_semantic_fingerprint_changes_when_derived_price_changes(client, session, perm):
    await _seed_items(client, perm["admin_h"], [{"name": "Derived", "sku": "DP-1", "sell_by": "piece", "quantity": "4"}], key="seed-dp")
    rows = [{"name": "Derived", "sku": "DP-1", "sell_by": "piece", "retail_price_total": "100"}]
    before = await _plan(session, perm["company_id"], rows, upsert=True)
    r = await _rows_commit(client, perm["admin_h"], [{"name": "Derived", "sku": "DP-1", "sell_by": "piece", "quantity": "5"}],
                           upsert=True, key="bump-dp")
    assert r.status_code == 200 and not r.json()["errors"], r.text
    after = await _plan(session, perm["company_id"], rows, upsert=True)
    assert before.errors == [] and after.errors == []
    assert before.semantic_fingerprint != after.semantic_fingerprint


async def test_semantic_fingerprint_changes_when_default_location_changes(client, session, perm):
    rows = [{"name": "Widget", "sell_by": "piece", "quantity": "1"}]
    before = await _plan(session, perm["company_id"], rows, upsert=False)
    r = await client.post("/companies/me/locations", json={"name": "Back", "type": "warehouse", "is_default": False},
                          headers=perm["admin_h"])
    assert r.status_code == 200, r.text
    r = await client.patch(f"/companies/me/locations/{r.json()['id']}", json={"is_default": True}, headers=perm["admin_h"])
    assert r.status_code == 200, r.text
    after = await _plan(session, perm["company_id"], rows, upsert=False)
    assert before.errors == [] and after.errors == []
    assert before.semantic_fingerprint != after.semantic_fingerprint


async def test_semantic_fingerprint_changes_when_category_default_changes(client, session, perm, monkeypatch):
    from celerp.services import vertical_presets
    rows = [{"name": "Stone", "category": "diamond", "quantity": "1"}]
    before = await _plan(session, perm["company_id"], rows, upsert=False)

    real_read_all = vertical_presets._read_all

    def _read_all(kind):
        items = real_read_all(kind)
        if kind == "categories":
            for c in items:
                if c.get("name") == "diamond":
                    c["default_sell_by"] = "carat"
        return items

    monkeypatch.setattr(vertical_presets, "_read_all", _read_all)
    after = await _plan(session, perm["company_id"], rows, upsert=False)
    assert before.semantic_fingerprint != after.semantic_fingerprint


async def test_bound_commit_refuses_clean_but_semantically_different_repreview(client, session, perm, monkeypatch):
    rows = [{"name": "Renamed", "sku": "UT-X", "sell_by": "piece"}]
    first_id = await _create_item(client, perm["admin_h"], perm["location_id"], sku="UT-X", name="First")
    preview = await _rows_preview(client, perm["admin_h"], rows, upsert=True, key="op-stale")
    assert preview["errors"] == []

    await _rename_sku(client, perm["admin_h"], first_id, "UT-X", "UT-Y")
    await _create_item(client, perm["admin_h"], perm["location_id"], sku="UT-X", name="Second")
    # The same rows still preview clean; they now mean a different item.
    assert (await _rows_preview(client, perm["admin_h"], rows, upsert=True, key="op-stale"))["errors"] == []

    writer = _writer_spy(monkeypatch)
    r = await _rows_commit(client, perm["admin_h"], rows, upsert=True, key="op-stale", preview_hash=preview["preview_hash"])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "preview_stale"
    writer.assert_not_awaited()
    assert (await _item_state_by_sku(session, perm["company_id"], "UT-X"))["name"] == "Second"


# ---------------------------------------------------------------------------
# Differential matrix: every transport, every owner case
# ---------------------------------------------------------------------------

# (case id, seed rows, rows, upsert, role header key, expected (field, code))
_MATRIX_CASES = [
    ("missing_name", None, [{"sell_by": "piece", "quantity": "1"}], False, "admin_h", [("name", "required")]),
    ("resolved_sell_by", None, [{"name": "Stone", "category": "diamond", "weight": "1.5", "weight_unit": "gram"}], False, "admin_h", []),
    ("unresolved_sell_by", None, [{"name": "Widget", "quantity": "1"}], False, "admin_h", [("sell_by", "sell_by_unresolved")]),
    ("invalid_unit", None, [{"name": "Widget", "sell_by": "furlong", "quantity": "1"}], False, "admin_h", [("sell_by", "sell_by_invalid")]),
    ("default_location", None, [{"name": "Widget", "sell_by": "piece", "quantity": "1"}], False, "admin_h", []),
    ("multiple_locations", None, [
        {"name": "Here", "sell_by": "piece", "quantity": "1", "location_name": "Main"},
        {"name": "There", "sell_by": "piece", "quantity": "1", "location_name": "Annex"},
    ], False, "admin_h", []),
    ("location_create_allowed", None, [{"name": "Widget", "sell_by": "piece", "quantity": "1", "location_name": "Annex"}], False, "admin_h", []),
    ("location_create_denied", None, [{"name": "Widget", "sell_by": "piece", "quantity": "1", "location_name": "Annex"}], False, "manager_h",
     [("location_name", "location_create_denied")]),
    ("unique_sku_upsert", [{"name": "One", "sku": "U-1", "sell_by": "piece", "quantity": "1"}],
     [{"name": "One renamed", "sku": "U-1", "sell_by": "piece"}], True, "admin_h", []),
    ("ambiguous_sku_upsert", [{"name": "A", "sku": "AMB", "sell_by": "piece", "quantity": "1"}, {"name": "B", "sku": "AMB", "sell_by": "piece", "quantity": "1"}],
     [{"name": "Which", "sku": "AMB", "sell_by": "piece"}], True, "admin_h", [("sku", "sku_ambiguous")]),
    ("shared_barcode", [{"name": "A", "sku": "SB-A", "barcode": "7508", "sell_by": "piece", "quantity": "1"}, {"name": "B", "sku": "SB-B", "barcode": "7508", "sell_by": "piece", "quantity": "1"}],
     [{"name": "Which", "barcode": "7508", "sell_by": "piece"}], True, "admin_h", [("barcode", "barcode_ambiguous")]),
    ("sku_barcode_conflict", [{"name": "A", "sku": "CX-A", "barcode": "1111", "sell_by": "piece", "quantity": "1"}, {"name": "B", "sku": "CX-B", "barcode": "2222", "sell_by": "piece", "quantity": "1"}],
     [{"name": "Mixed", "sku": "CX-A", "barcode": "2222", "sell_by": "piece"}], True, "admin_h", [("sku", "sku_barcode_conflict")]),
    ("sell_by_change_without_quantity", [{"name": "A", "sku": "SC-1", "sell_by": "piece", "quantity": "1"}],
     [{"name": "A", "sku": "SC-1", "sell_by": "gram"}], True, "admin_h", [("sell_by", "sell_by_change_needs_quantity")]),
    ("price_total_derivation", None, [{"name": "Widget", "sell_by": "piece", "pieces": "4", "retail_price_total": "100"}], False, "admin_h", []),
]

_TRANSPORTS = ["bound_rows", "direct_rows", "file"]


@pytest.mark.parametrize("transport", _TRANSPORTS)
@pytest.mark.parametrize("case", _MATRIX_CASES, ids=[c[0] for c in _MATRIX_CASES])
async def test_preview_commit_differential_matrix(client, session, perm, monkeypatch, write_upload, case, transport):
    case_id, seed, rows, upsert, who, expected = case
    h = perm[who]
    key = f"op-{case_id}"
    if seed:
        await _seed_items(client, perm["admin_h"], seed, key=f"seed-{case_id}")

    if transport == "file":
        fid = write_upload(perm["company_id"], _user_id(h), _csv_text(rows))
        cols = next(csv.reader(io.StringIO(_csv_text(rows))))
        mapping = {c: c for c in cols}
        r = await client.post("/items/import/preview", json={"file_id": fid, "upsert": upsert, "mapping": mapping}, headers=h)
        assert r.status_code == 200, r.text
        preview = r.json()
        expected_rows = preview["sample"]
        expected_key = f"preview:{preview['preview_hash']}"
    else:
        preview = await _rows_preview(client, h, rows, upsert=upsert, key=key)
        expected_rows, expected_key = rows, key
    assert _fc(preview["errors"]) == sorted(expected), preview["errors"]

    writer = _writer_spy(monkeypatch)
    entry = _import_items_spy(monkeypatch)
    preflight = _preflight_spy(monkeypatch)
    before = await _item_count(session, perm["company_id"])

    if transport == "file":
        r = await client.post("/items/import/commit", json={
            "file_id": fid, "upsert": upsert, "mapping": mapping, "preview_hash": preview["preview_hash"],
        }, headers=h)
    elif transport == "bound_rows":
        r = await _rows_commit(client, h, rows, upsert=upsert, key=key, preview_hash=preview["preview_hash"])
    else:
        r = await _rows_commit(client, h, rows, upsert=upsert, key=key)

    if expected:
        assert _rfc(_rejected_errors(r)) == _rfc(preview["errors"])
        writer.assert_not_awaited()
        assert await _item_count(session, perm["company_id"]) == before
        return

    assert r.status_code == 200, r.text
    assert r.json()["errors"] == []
    assert entry.await_count == 1
    assert entry.await_args.args[5] == expected_rows
    assert entry.await_args.kwargs["upsert"] is upsert
    assert entry.await_args.kwargs["idempotency_key"] == expected_key
    assert preflight.await_count >= 1
    assert preflight.await_args.args[4] == expected_rows
    assert writer.await_count >= 1


# ---------------------------------------------------------------------------
# Every semantic transport writes through import_items and its preflight
# ---------------------------------------------------------------------------


async def test_all_semantic_transports_write_through_import_items(client, session, perm, monkeypatch, write_upload, tmp_path):
    import celerp.main
    import ui.api_client as api
    import ui.config
    from ui.app import app as ui_app
    from ui.routes import csv_import as ci
    from ui.routes.inventory import _import_operation_key

    entry = _import_items_spy(monkeypatch)
    preflight = _preflight_spy(monkeypatch)
    h = perm["admin_h"]

    # Direct rows.
    r = await _rows_commit(client, h, [{"name": "Direct", "sell_by": "piece", "quantity": "1"}], key="op-t-direct")
    assert r.status_code == 200, r.text
    assert entry.await_count == 1
    preflights = {"direct_rows": preflight.await_count}

    # File commit.
    fid = write_upload(perm["company_id"], perm["admin_user_id"], "name,sell_by,quantity\nFiled,piece,1\n")
    r = await client.post("/items/import/preview", json={"file_id": fid, "upsert": False}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.post("/items/import/commit", json={"file_id": fid, "upsert": False, "preview_hash": r.json()["preview_hash"]}, headers=h)
    assert r.status_code == 200, r.text
    assert entry.await_count == 2
    preflights["file"] = preflight.await_count

    # Browser confirm, with the browser's client routed to this API.
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    monkeypatch.setattr(ui.config, "API_BASE", "http://api")
    monkeypatch.setattr(api, "_get_bulk_transport", lambda: ASGITransport(app=celerp.main.app))
    monkeypatch.setattr(api, "_get_transport", lambda: ASGITransport(app=celerp.main.app))
    text = "name,sell_by,quantity\nBrowsed,piece,1\n"
    browser_rows = list(csv.DictReader(io.StringIO(text)))
    ref = ci._write_stage(perm["company_id"], text)
    preview = await _rows_preview(client, h, browser_rows, key=_import_operation_key(browser_rows, False))
    token = h["Authorization"].split()[1]
    with patch.object(api, "get_company", new=AsyncMock(return_value={
        "id": perm["company_id"], "current_role": "owner", "settings": await _company_settings(session, perm["company_id"]),
    })):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as c:
            r = await c.post("/inventory/import/confirm", data={"csv_ref": ref, "preview_hash": preview["preview_hash"]},
                             cookies={"celerp_token": token})
    assert r.status_code == 200, r.text
    assert entry.await_count == 3
    assert entry.await_args.args[5] == browser_rows
    preflights["browser_confirm"] = preflight.await_count

    # Each transport ran the preflight at least once of its own.
    assert preflights["direct_rows"] >= 1, preflights
    assert preflights["file"] > preflights["direct_rows"], preflights
    assert preflights["browser_confirm"] > preflights["file"], preflights
