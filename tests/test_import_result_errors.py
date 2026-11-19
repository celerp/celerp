# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""What the chart, locations, taxes and payment terms import results show.

A batch endpoint reports incomplete success with the error messages, or only a
number of failed rows. The result page shows the messages whenever there are any,
shows the failed-row count only when that is all the endpoint said, and keeps the
staged file in both cases so the user can go back and retry.

Every refused row a batch route reports is a refusal with its catalog key
(``import.record_refused`` naming the row, the reason inside it keyed too), so it
reads in each reader's language: no shipped import writes it as failure_reason
text, and every batch route of the app is driven here with rows it refuses, so a
new batch route fails until its refused rows are listed below.
"""

from __future__ import annotations

import ast
import re
from unittest.mock import AsyncMock

import pytest

from celerp.main import app
from celerp.services import import_stage

from peer_attention_support import stored_keys
from test_import_stage_invariants import _IMPORTERS, _REPO, _calls, _confirm, _route_path
from ui.routes import csv_import as ci
from ui.i18n import t

_RESULT_IMPORTERS = ["chart", "locations", "taxes", "terms"]

_ERRORS_ONLY = {"created": 0, "skipped": 0, "errors": ["Zeta VAT: Invalid rate"]}
_FAILED_ONLY = {"created": 0, "skipped": 0, "failed": 2}
_BOTH = {"created": 0, "skipped": 0, "failed": 1, "errors": ["Zeta VAT: Invalid rate"]}
_CLEAN = {"created": 1, "skipped": 0, "errors": []}


@pytest.fixture
def stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path / "import_staging"


async def _result(name: str, result: dict):
    ref, r = await _confirm(name, AsyncMock(return_value=dict(result)))
    assert r.status_code == 200, r.text
    return ref, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_renders_actual_errors(name, stage_dir):
    _, html = await _result(name, _ERRORS_ONLY)
    assert "Zeta VAT: Invalid rate" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_failed_count_rendered_when_it_is_the_only_signal(name, stage_dir):
    _, html = await _result(name, _FAILED_ONLY)
    assert t("settings_import.records_failed", "en", n=2) in html


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_failed_count_not_rendered_when_errors_are_present(name, stage_dir):
    _, html = await _result(name, _BOTH)
    assert "Zeta VAT: Invalid rate" in html
    assert t("settings_import.records_failed", "en", n=2) not in html


def test_helper_accepts_errors_list_and_failed_count():
    assert ci.import_result_errors(_ERRORS_ONLY) == ["Zeta VAT: Invalid rate"]
    assert ci.import_result_errors(_FAILED_ONLY) == [t("settings_import.records_failed", "en", n=2)]
    assert ci.import_result_errors(_BOTH) == ["Zeta VAT: Invalid rate"]
    assert ci.import_result_errors(_CLEAN) == []
    assert ci.import_result_errors({"created": 3, "failed": 0}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_keeps_stage_on_errors(name, stage_dir):
    ref, _ = await _result(name, _ERRORS_ONLY)
    assert import_stage.read_stage("company-a", ref) == _IMPORTERS[name]["csv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_keeps_stage_on_failed_count(name, stage_dir):
    ref, _ = await _result(name, _FAILED_ONLY)
    assert import_stage.read_stage("company-a", ref) == _IMPORTERS[name]["csv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_discards_stage_on_clean_result(name, stage_dir):
    ref, _ = await _result(name, _CLEAN)
    assert import_stage.read_stage("company-a", ref) is None


def test_four_confirm_routes_use_the_one_helper():
    """Each confirm route builds its result errors with the shared helper, never its own copy."""
    wanted = {_IMPORTERS[name]["confirm"] for name in _RESULT_IMPORTERS}
    seen = set()
    for path in ("accounting_import.py", "settings_import.py"):
        for node in ast.walk(ast.parse((_REPO / "ui" / "routes" / path).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            routes = {p for p in map(_route_path, node.decorator_list) if p} & wanted
            if not routes:
                continue
            seen |= routes
            assert "import_result_errors" in _calls(node), f"{routes} builds its own result errors"
            assert "records_failed" not in ast.unparse(node), f"{routes} formats the failed count itself"
    assert seen == wanted


def test_every_result_panel_names_its_records_in_the_users_language():
    """The result's "View ..." button names the imported records through the catalog
    ("Lager anzeigen" in German), never an English literal."""
    callers = []
    for path in sorted((_REPO / "ui" / "routes").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and "import_result_panel" in ast.unparse(node.func):
                label = next(k.value for k in node.keywords if k.arg == "entity_label")
                callers.append((path.name, node.lineno, isinstance(label, ast.Constant)))
    assert callers and not [c for c in callers if c[2]], callers


def _writes_a_refused_record(call: ast.Call) -> bool:
    """A call that records a refused row or record: an ``errors.append``, a RecordOutcome,
    an outcome's ``add``, or a sink error."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in ("RecordOutcome", "SinkError", "sink_error")
    return isinstance(func, ast.Attribute) and (
        func.attr == "add" or (func.attr == "append" and isinstance(func.value, ast.Name) and func.value.id == "errors"))


def test_a_refused_import_row_keeps_its_keyed_refusal():
    """A refused row or migration record carries the refusal the write raised (row_refusal),
    so it reads in each reader's language: no shipped import or migration sink writes it as
    the English text of failure_reason."""
    from shipped_sources import shipped_sources

    texts = []
    for rel, path in shipped_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and _writes_a_refused_record(node)
                    and any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == "failure_reason"
                            for n in ast.walk(node))):
                texts.append(f"{rel}:{node.lineno}")
    assert not texts, f"a refused import row is written as text, use row_refusal: {texts}"


def _sentence(node: ast.AST) -> bool:
    """English prose written in place: a string, or an f-string, with a space in its text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return " " in node.value.strip()
    return isinstance(node, ast.JoinedStr) and any(
        isinstance(v, ast.Constant) and " " in str(v.value) for v in node.values)


def test_a_migration_sink_names_every_refused_record_with_a_catalog_key():
    """A migration sink's reasons for a record it cannot import, and the record names in them,
    are catalog refusals (migration.record.*, migration.label.*), so a failed batch reads in
    each reader's language: no sink returns a reason, or hands a refused-record writer one,
    as English it wrote itself."""
    from shipped_sources import shipped_sources

    texts = []
    for rel, path in shipped_sources():
        if path.name not in ("migration_sink.py", "migration_core_sink.py"):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and (_writes_a_refused_record(node)
                                               or getattr(node.func, "id", "") in ("row_refusal", "record_refused")):
                parts = node.args
            elif isinstance(node, ast.Return) and node.value is not None:
                parts = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
            else:
                continue
            texts += [f"{rel}:{node.lineno}" for part in parts if _sentence(part)]
    assert not texts, f"a refused migration record is written as English text, use catalog_refusal: {texts}"


_BATCH = re.compile(r"/import/(batch|preview)$")


def _batch_rec(entity_id: str, event_type: str, data: dict | None = None) -> dict:
    return {"entity_id": entity_id, "event_type": event_type, "data": data or {},
            "source": "import", "idempotency_key": f"keyed-rows:{entity_id}"}


def _settings(*rows: dict) -> dict:
    return {"records": [_batch_rec(f"row-{i}", "settings.row", row) for i, row in enumerate(rows)]}


_TAX_ROWS = _settings({"name": ""}, {"name": "Bad rate", "rate": "seven"},
                      {"name": "Bad type", "rate": 7, "tax_type": "sometimes"})
_TERM_ROWS = _settings({"name": ""}, {"name": "Bad days", "days": "thirty"})
_CHART_ROWS = {"records": [
    "not a row",
    {"code": "", "name": "No code", "account_type": "asset"},
    {"code": "9101", "name": "Twice", "account_type": "asset"},
    {"code": "9101", "name": "Twice again", "account_type": "asset"},
    {"code": "9102", "name": "Orphan", "account_type": "asset", "parent_code": "9999"},
    {"code": "9103", "name": "Under orphan", "account_type": "asset", "parent_code": "9102"},
    {"code": "9104", "name": "Loop A", "account_type": "asset", "parent_code": "9105"},
    {"code": "9105", "name": "Loop B", "account_type": "asset", "parent_code": "9104"},
]}

# Each batch route, and rows it refuses.
REFUSED_ROWS: dict[str, dict] = {
    "/companies/import/batch": {"records": [_batch_rec("co-1", "sys.user.created")]},
    "/companies/me/locations/import/batch": {"records": [{"name": ""}]},
    "/companies/me/taxes/import/batch": _TAX_ROWS,
    "/companies/me/purchasing-taxes/import/batch": _TAX_ROWS,
    "/companies/me/payment-terms/import/batch": _TERM_ROWS,
    "/companies/me/purchasing-payment-terms/import/batch": _TERM_ROWS,
    "/accounting/accounts/import/preview": _CHART_ROWS,
    "/accounting/accounts/import/batch": _CHART_ROWS,
    "/accounting/import/batch": {"records": [_batch_rec("je-1", "acc.account.created")]},
    "/crm/contacts/import/batch": {"records": [
        _batch_rec("ct-1", "crm.contact.deleted"),
        _batch_rec("ct-2", "crm.contact.created", {"name": "Us", "is_self": True}),
    ]},
    "/docs/import/batch": {"records": [_batch_rec("doc-1", "doc.voided")]},
    "/lists/import/batch": {"records": [_batch_rec("list-1", "list.voided")]},
    "/manufacturing/import/batch": {"records": [_batch_rec("mfg-1", "mfg.order.completed")]},
    "/items/import/batch": {"records": [_batch_rec("item-1", "item.deleted")]},
}


def _batch_routes() -> set[str]:
    # The item and subscription previews check a file's cells before any row is written;
    # they answer cell checks, not refused rows.
    return {path for path, ops in app.openapi()["paths"].items() if "post" in ops and _BATCH.search(path)
            and not path.startswith(("/items/import/preview", "/subscriptions/"))}


def _keyed(error) -> bool:
    if not (isinstance(error, dict) and error.get("message_key") and error.get("message")):
        return False
    inner = (error.get("params") or {}).get("refusal")
    return error["message_key"] != "import.record_refused" or (isinstance(inner, dict) and bool(inner.get("message_key")))


@pytest.mark.asyncio
async def test_every_batch_route_has_refused_rows_here(client):
    # The client mounts every bundled module's routes.
    assert _batch_routes() == set(REFUSED_ROWS)


# Each route that runs on chosen records (an ``entity_ids`` list): the test proving every
# record it refuses is a keyed refusal, or why it answers no refused record of its own.
CHOSEN_RECORD_ROUTES: dict[str, str] = {
    "/connector-items/{connector_name}/sync":
        "test_channel_link_truth.py::test_a_refused_store_sync_item_is_a_keyed_refusal",
    "/items/bulk/status": "refuses the whole request",
    "/items/bulk/make-available": "refuses the whole request",
    "/items/bulk/revert-to-draft": "refuses the whole request",
    "/items/bulk/delete": "refuses the whole request",
    "/items/bulk/restore-deleted": "refuses the whole request",
    "/items/bulk/expire": "refuses the whole request",
    "/items/bulk/transfer": "a refused lot carries the write's own refusal detail as raised",
    "/items/metadata": "a read",
    "/api/labels/bulk-print": "refuses the whole request",
    "/docs/accounting-sync/bulk": "refuses the whole request",
    "/lists/writeoff": "refuses the whole request",
}


def _chosen_record_routes() -> set[str]:
    schemas = app.openapi()["components"]["schemas"]
    found = set()
    for path, ops in app.openapi()["paths"].items():
        body = (ops.get("post", {}).get("requestBody") or {}).get("content", {}).get("application/json", {})
        ref = (body.get("schema") or {}).get("$ref", "")
        props = schemas.get(ref.rsplit("/", 1)[-1], {}).get("properties", {}) if ref else {}
        if props.get("entity_ids", {}).get("type") == "array":
            found.add(path)
    return found


@pytest.mark.asyncio
async def test_every_route_on_chosen_records_answers_refused_records_keyed(client):
    """A new route on chosen records names here the test that its refused records are
    keyed refusals, so none answers a refused record as English-only text."""
    from pathlib import Path

    assert _chosen_record_routes() == set(CHOSEN_RECORD_ROUTES)
    tests = Path(__file__).parent
    for target in CHOSEN_RECORD_ROUTES.values():
        if "::" in target:
            name, test = target.split("::")
            assert f"def {test}(" in (tests / name).read_text(encoding="utf-8"), target


@pytest.mark.asyncio
@pytest.mark.parametrize("path", sorted(REFUSED_ROWS))
async def test_a_refused_row_is_a_keyed_refusal(client, auth, path):
    r = await client.post(path, headers=auth["headers"], json=REFUSED_ROWS[path])
    assert r.status_code == 200, r.text
    errors = r.json().get("errors")
    assert errors, r.text
    assert [e for e in errors if not _keyed(e)] == []


async def _keyed_http(session, **kw):
    from fastapi import HTTPException
    from celerp.accounting_roles import catalog_refusal
    raise HTTPException(422, catalog_refusal("peer.err_connected_company_s_record_needs_its"))


async def _plain_value(session, **kw):
    raise ValueError("Quantity must be a number")


async def _unexpected(session, **kw):
    raise RuntimeError("connection reset")


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver, revision", [
    (_keyed_http, 1), (_plain_value, 1), (_unexpected, 1), (_keyed_http, None)])
async def test_a_record_a_connected_company_sent_is_refused_with_a_keyed_reason(
        session, auth, ids, monkeypatch, receiver, revision):
    """A connected company's record the receiving company does not apply comes back, like
    any batch route's refused row, as a keyed refusal naming it: the sender shows it, and
    the receiver's attention list keeps it, in each reader's language."""
    import uuid
    from types import SimpleNamespace
    from celerp.services import peer_access, peer_import

    grant = SimpleNamespace(company_id=ids["company_id"], source_company_id=uuid.uuid4(), last_used_at=None)

    async def _grant(session, peer):
        return grant

    monkeypatch.setattr(peer_import, "_grant", _grant)
    monkeypatch.setitem(peer_import.RECEIVERS, "contacts", receiver)
    peer = peer_access.PeerCaller(credential="k", source_company_id=grant.source_company_id, scope="contacts")
    record = SimpleNamespace(entity_id="C-1", source_revision=revision, data={"name": "C"})

    errors = (await peer_import.receive_records(session, peer, [record]))["errors"]

    assert len(errors) == 1 and _keyed(errors[0]), errors
    assert errors[0]["params"]["entity_id"] == "C-1"
    assert await stored_keys(session, ids["company_id"]), "the attention row keeps the reason keyed"


async def _attention_reason(session, company_id) -> dict:
    """The one open attention item's reason as its column stores it."""
    import json
    from sqlalchemy import select
    from celerp.models.company_peer import CompanyPeerAttention

    row = (await session.scalars(select(CompanyPeerAttention).where(
        CompanyPeerAttention.company_id == company_id))).one()
    try:
        return json.loads(row.reason)
    except ValueError:
        return {"text": row.reason}


def test_a_peer_attention_reason_is_stored_only_as_a_keyed_refusal():
    """The boundary every attention writer passes its reason through (record, or stored for
    an item changed in place): English alone is refused and a keyed refusal reads back by its
    key. Each writer's end-to-end scenario reads its stored row back through
    peer_attention_support, so a writer that stores English fails the scenario driving it."""
    from celerp.accounting_roles import catalog_refusal
    from celerp.services import peer_attention
    from peer_attention_support import stored_key

    for english in ("Fixed. It posts on the next sync.", {"message": "Fixed."}):
        with pytest.raises(TypeError):
            peer_attention.stored(english)
    assert stored_key(peer_attention.stored(catalog_refusal("sync_accounts.fixed"))) == "sync_accounts.fixed"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["Accounts reviewed.", {"message": "Accounts reviewed."},
                                    {"message": "x", "message_key": "error.refusal_as_written", "params": {"text": "x"}}])
async def test_a_peer_attention_reason_that_is_not_a_keyed_refusal_is_refused(session, auth, ids, reason):
    import uuid
    from celerp.services import peer_attention

    attempt = peer_attention.record(session, company_id=ids["company_id"], source_company_id=uuid.uuid4(),
                                    entity_type="contacts", source_entity_id="C-1", label="C", reason=reason)
    if isinstance(reason, dict) and reason.get("message_key"):
        await attempt  # the generic key carrying a text as written is keyed
        assert _keyed(await _attention_reason(session, ids["company_id"]))
        return
    with pytest.raises(TypeError):
        await attempt


@pytest.mark.asyncio
async def test_a_journal_the_connected_company_declined_keeps_a_keyed_reason(session, auth, ids, monkeypatch):
    import uuid
    from types import SimpleNamespace
    from celerp.services import peer_journals

    async def _firm_journal(session, company_id, je_id):
        return {"memo": "Adjust"}

    monkeypatch.setattr(peer_journals, "_firm_journal", _firm_journal)
    grant = SimpleNamespace(company_id=ids["company_id"], source_company_id=uuid.uuid4(), label="Demo Trading")
    await peer_journals._answer(session, grant, peer_journals._Answer(journal="J-1", declined="Wrong period"))

    reason = await _attention_reason(session, ids["company_id"])
    assert _keyed(reason) and reason["message_key"] == "peer.attention_declined", reason
    assert reason["params"] == {"client": "Demo Trading", "why": "Wrong period"}


def test_a_peer_attention_reason_reads_in_the_readers_language_and_an_older_one_as_written():
    import json
    from types import SimpleNamespace
    from celerp.accounting_roles import catalog_refusal
    from celerp.services import peer_attention
    from ui import i18n

    keyed = SimpleNamespace(reason=json.dumps(catalog_refusal("peer.attention_accounts_reviewed")))
    older = SimpleNamespace(reason="Accounts reviewed. It posts on the next sync.")
    token = i18n._current_lang.set("th")
    try:
        assert i18n.refusal_text(peer_attention.reason_of(keyed)) == t("peer.attention_accounts_reviewed", "th")
        assert i18n.refusal_text(peer_attention.reason_of(older)) == older.reason
    finally:
        i18n._current_lang.reset(token)
