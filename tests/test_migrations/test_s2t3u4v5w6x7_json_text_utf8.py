# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""JSON is stored as UTF-8 text, never as \\u escapes past ASCII, which a SQL_ASCII database
cannot read back. A database an earlier version filled with both forms, Thai text in a number
and in other fields, upgrades from before the first index on projection state to head on every
encoding Celerp creates, and then saves, numbers, imports, backs up and restores."""

from __future__ import annotations

import ast
import asyncio
import json
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from celerp.json_text import SYNC_TEXT, dump_json
from company_backup_support import company, owner, settle
from shipped_sources import shipped_sources
from test_company_backup import _bk_modules_running  # noqa: F401
from .conftest import throwaway_db, upgrade_to
from .test_r1s2t3u4v5w6_projections_canonical_number_lookup import ENCODINGS

# The revision before the first expression index on projection state (ix_projections_connector_key).
BEFORE_STATE_INDEX = "c2d3e4f5a6b7"
# The head of the latest release: the escape rewrite is the first revision after it.
RELEASED_HEAD = "n9b0c1d2e3f4"
THAI = "ใบแจ้งหนี้"


def _sql(sync_url: str, sql: str, params: dict | None = None) -> list[tuple]:
    engine = create_engine(sync_url, **SYNC_TEXT)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params or {})
            return [tuple(r) for r in result] if result.returns_rows else []
    finally:
        engine.dispose()


def _seed_both_forms(sync_url: str) -> str:
    """A company and two Documents as earlier versions saved them: one with every character past
    ASCII written as a \\u escape (json.dumps' default), one as UTF-8 text, each with a Thai
    number padded with a space past ASCII and a Thai customer name."""
    cid = _sql(sync_url, "INSERT INTO companies (id, name, slug, settings, created_at) VALUES "
                         "(gen_random_uuid(), 'Demo', 'demo', CAST(:s AS json), now()) RETURNING id",
               {"s": json.dumps({"company_address": THAI})})[0][0]
    for key, number, dump in (("doc:escaped", f" {THAI}-1 ", json.dumps),
                              ("doc:raw", f"　{THAI}-2　", dump_json)):
        _sql(sync_url, "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                       "VALUES (:c, :e, 'doc', CAST(:s AS json), 1, now())",
             {"c": cid, "e": key, "s": dump({"doc_type": "invoice", "ref_id": number, "doc_number": number,
                                            "contact_name": THAI})})
    return str(cid)


async def _number(async_url: str, cid: str) -> tuple[list, str, int]:
    """(holder of each seeded number, the number an import of a new one stores, the refusal
    status of an import of a held one)."""
    from celerp_docs.routes import free_import_numbers
    from celerp_docs.sequences import held_numbers, number_holder

    engine = create_async_engine(async_url, json_serializer=dump_json)
    try:
        async with AsyncSession(engine) as session:
            holders = [await number_holder(session, cid, f"{THAI}-{n}") for n in (1, 2)]
            await held_numbers(session, cid, {}, ["invoice"])
            stored = (await free_import_numbers(session, cid, f"doc:{uuid.uuid4()}",
                                                {"ref_id": f" {THAI}-3 "}))["ref_id"]
            try:
                await free_import_numbers(session, cid, f"doc:{uuid.uuid4()}", {"ref_id": f"{THAI}-1"})
                refused = 200
            except HTTPException as exc:
                refused = exc.status_code
            await session.rollback()
    finally:
        await engine.dispose()
    return holders, stored, refused


def _assert_readable(sync_url: str, cid: str) -> None:
    """Every json value reads back with ->>, holds its characters as UTF-8 text, numbers stripped."""
    rows = _sql(sync_url, "SELECT entity_id, state ->> 'ref_id', state ->> 'contact_name', CAST(state AS text) "
                          "FROM projections WHERE company_id = :c ORDER BY entity_id", {"c": cid})
    assert [r[:3] for r in rows] == [("doc:escaped", f"{THAI}-1", THAI), ("doc:raw", f"{THAI}-2", THAI)]
    assert all(THAI in r[3] and "\\u0e" not in r[3] for r in rows), rows
    assert _sql(sync_url, "SELECT settings ->> 'company_address' FROM companies") == [(THAI,)]


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_a_database_with_escaped_and_raw_thai_upgrades_numbers_imports_and_restores(encoding, tmp_path):
    from celerp.services import backup

    with throwaway_db("jsontext", encoding) as (async_url, sync_url), \
            throwaway_db("jsonrest", encoding) as (rest_async, rest_url):
        upgrade_to(sync_url, BEFORE_STATE_INDEX)
        cid = _seed_both_forms(sync_url)

        upgrade_to(sync_url, "head")

        _assert_readable(sync_url, cid)
        holders, stored, refused = asyncio.run(_number(async_url, cid))
        assert holders == [("doc", f"{THAI}-1"), ("doc", f"{THAI}-2")]
        assert (stored, refused) == (f"{THAI}-3", 409)
        # A save writes UTF-8 text, which every later query reads.
        _sql(sync_url, "UPDATE projections SET state = CAST(:s AS json) WHERE entity_id = 'doc:raw'",
             {"s": dump_json({"doc_type": "invoice", "ref_id": f"{THAI}-2", "contact_name": THAI})})
        _assert_readable(sync_url, cid)

        dump = tmp_path / "thai.dump"
        backup.dump_database(sync_url.replace("+psycopg2", ""), dump)
        backup.restore_database_file(dump, rest_url.replace("+psycopg2", ""))
        _assert_readable(rest_url, cid)
        assert asyncio.run(_number(rest_async, cid))[0] == holders


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_unescape_json_text_rewrites_escapes_on_utf8(encoding):
    """On every encoding, not SQL_ASCII alone, and a second run changes nothing."""
    from celerp.migrations._json_compat import unescape_json_text

    with throwaway_db("jsonidem", encoding) as (_, sync_url):
        upgrade_to(sync_url, BEFORE_STATE_INDEX)
        _seed_both_forms(sync_url)
        engine = create_engine(sync_url, **SYNC_TEXT)
        try:
            with engine.begin() as conn:
                assert unescape_json_text(conn) == 2  # the escaped Document and the company
            with engine.begin() as conn:
                assert unescape_json_text(conn) == 0
        finally:
            engine.dispose()


def test_escaped_journal_entry_metadata_is_rewritten_before_any_index_reads_it():
    """A journal entry an earlier version saved with a \\u escape past ASCII in its metadata, which
    the first index after the release reads as jsonb, upgrades a SQL_ASCII database to head."""
    with throwaway_db("jsonje", "SQL_ASCII") as (_, sync_url):
        upgrade_to(sync_url, RELEASED_HEAD)
        cid = _sql(sync_url, "INSERT INTO companies (id, name, slug, settings, created_at) VALUES "
                             "(gen_random_uuid(), 'Demo', 'demo', '{}', now()) RETURNING id")[0][0]
        _sql(sync_url, "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, metadata, "
                       "source, idempotency_key) VALUES (:c, 'je:1', 'journal_entry', 'acc.journal_entry.created', "
                       "'{}', CAST(:m AS json), 'api', 'k-je-1')", {"c": cid, "m": json.dumps({"memo": THAI})})

        upgrade_to(sync_url, "head")

        assert _sql(sync_url, "SELECT metadata ->> 'memo', CAST(metadata AS jsonb) ->> 'memo' FROM ledger") == [
            (THAI, THAI)]


async def test_a_company_backup_with_thai_text_restores_on_a_sql_ascii_database(tmp_path, monkeypatch,
                                                                             _bk_modules_running):
    """A company and a Document holding Thai text, backed up and restored as a new company,
    on a SQL_ASCII database: every row the restore writes is sent as UTF-8 text."""
    import celerp.db
    from celerp.config import settings
    from celerp.services import company_backup

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with throwaway_db("jsoncb", "SQL_ASCII") as (async_url, sync_url):
        await asyncio.to_thread(upgrade_to, sync_url, "head")
        engine = create_async_engine(async_url, json_serializer=dump_json, poolclass=NullPool)
        monkeypatch.setattr(celerp.db, "engine", engine)
        try:
            await celerp.db.create_tables(engine)  # module tables, as boot creates them
            user = await owner(engine)
            cid = await company(engine, user, THAI, "thai-backup")
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c"),
                                   {"s": dump_json({"company_address": THAI}), "c": cid})
                await conn.execute(text(
                    "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, source, idempotency_key) "
                    "VALUES (:c, 'doc:thai', 'doc', 'doc.created', CAST(:d AS json), 'api', 'k-thai')"),
                    {"c": cid, "d": dump_json({"doc_type": "quotation", "ref_id": f"{THAI}-1", "contact_name": THAI,
                                               "line_items": [], "total": 0})})
            await settle(engine, cid)
            backup = tmp_path / "thai.celerp-company"
            await company_backup.export_company_snapshot(cid, backup)
            async with AsyncSession(engine) as session:
                plan = await company_backup.plan_existing_restore(
                    session, company_backup.read_backup(backup), "new_company", user, None)
            await company_backup.restore_company(backup, mode="new_company", user_id=user,
                                                 plan_fingerprint=plan.fingerprint)
        finally:
            await engine.dispose()
        assert _sql(sync_url, "SELECT state ->> 'contact_name' FROM projections WHERE entity_id = 'doc:thai'") == [
            (THAI,), (THAI,)]


_SYNC_FACTORIES = {"create_engine", "engine_from_config"}


def _passes_sync_text(call: ast.Call, rel: str) -> bool:
    """**SYNC_TEXT; or **MIGRATION_TEXT in the engine env.py migrates through; or the fence engine's
    own **text, whose default is SYNC_TEXT."""
    names = {getattr(k.value, "id", None) for k in call.keywords if k.arg is None}
    return "SYNC_TEXT" in names or rel == "celerp/migrations/env.py" and "MIGRATION_TEXT" in names or (
        rel == "celerp/migrations/compatibility.py" and "text" in names)


def test_every_engine_writes_json_as_utf8_text():
    """Every engine the app or a migration opens serializes JSON with dump_json, so no writer
    stores a \\u escape past ASCII; a psycopg2 engine also talks UTF-8 (SYNC_TEXT), else it could
    neither send that text to a SQL_ASCII database nor read its bytes back."""
    missing = []
    for rel, path in shipped_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = getattr(node.func, "attr", getattr(node.func, "id", None)) if isinstance(node, ast.Call) else None
            if name in _SYNC_FACTORIES and not _passes_sync_text(node, rel) or name == "create_async_engine" and not any(
                    k.arg == "json_serializer" for k in node.keywords):
                missing.append(f"{rel}:{node.lineno}")
    assert not missing, missing


_DIGEST = "hashed or compared, never stored as JSON in the database"
_FILE = "written to a file and read back with json.loads"
_WIRE = "sent over HTTP, a stream or a log line, never stored in the database"
_PAGE = "written into a page the browser reads, never stored in the database"
# Every json.dumps in the app's sources (shipped_sources) that may write \u escapes, by (file, function), and why that is
# safe: none of them puts JSON into the database, where dump_json is the one writer.
_ASCII_DUMPS = {
    ("celerp/ai/files.py", "save_upload"): _FILE,
    ("celerp/ai/jobs.py", "digest"): _DIGEST,
    ("celerp/ai/llm.py", "_relay_request"): _WIRE,
    ("celerp/ai/llm.py", "operations_status"): _WIRE,
    ("celerp/ai/llm.py", "complete"): _WIRE,
    ("celerp/config_store.py", "merge_packaged_config"): _FILE,
    ("celerp/connectors/first_sync.py", "_version"): _DIGEST,
    ("celerp/events/engine.py", "connector_upsert"): _DIGEST,
    ("celerp/gateway/client.py", "_send"): _WIRE,
    ("celerp/gateway/state.py", "_persist_commercial_context"): _FILE,
    ("celerp/help/checks.py", "<module>"): _WIRE,
    ("celerp/middleware.py", "log_unhandled_exception"): _WIRE,
    ("celerp/migrations/compatibility.py", "to_json"): _WIRE,
    ("celerp/migrations/versions/k5e6f7a8b9c0_connector_entity_directions.py", "upgrade"):
        "a released revision; it writes connector names and directions, which are ASCII",
    ("celerp/modules/license.py", "_write_verdict"): _FILE,
    ("celerp/modules/license.py", "exchange_api_key_for_jwt"): _WIRE,
    ("celerp/modules/license.py", "_verify_remote"): _WIRE,
    ("celerp/modules/license.py", "_write_cache"): _FILE,
    ("celerp/modules/marketplace_stage.py", "write_stage"): _FILE,
    ("celerp/modules/meta.py", "write_meta"): _FILE,
    ("celerp/modules/outcome.py", "publish"): _FILE,
    ("celerp/modules/outcome.py", "report_stopped"): _FILE,
    ("celerp/modules/outcome.py", "apply_reported_stops"): _FILE,
    ("celerp/routers/events.py", "_stream"): _WIRE,
    ("celerp/services/backup_export.py", "_build_archive"): _FILE,
    ("celerp/services/backup_import.py", "_prepare_sync"): _FILE,
    ("celerp/services/backup_import.py", "_write_restore_notice"): _FILE,
    ("celerp/services/backup_import.py", "run_recovery"): _FILE,
    ("celerp/services/backup_repo.py", "run_snapshot"): _FILE,
    ("celerp/services/backup_repo.py", "reassemble_snapshot"): _FILE,
    ("celerp/services/company_backup.py", "_row_digest"): _DIGEST,
    ("celerp/services/company_backup.py", "_export_company"): _FILE,
    ("celerp/services/company_backup.py", "digest"): _DIGEST,
    ("celerp/services/company_backup.py", "_check_replacing"): _DIGEST,
    ("celerp/services/company_backup_files.py", "_write_json"): _FILE,
    ("celerp/services/company_lock.py", "lock_projections"): _DIGEST,
    ("celerp/services/field_fingerprints.py", "fingerprint"): _DIGEST,
    ("celerp/services/import_stage.py", "write_stage"): _FILE,
    ("celerp/services/import_stage.py", "update_draft"): _FILE,
    ("celerp/services/module_journals.py", "_intent_hash"): _DIGEST,
    ("celerp/services/nav_overrides.py", "revision"): _DIGEST,
    ("celerp/services/peer_documents.py", "receive_document"): "measures the size of a request",
    ("celerp/services/peer_payments.py", "apply_source_payments"): _DIGEST,
    ("celerp/services/staged_downloads.py", "stage"): _FILE,
    ("celerp/services/update.py", "write_state"): _FILE,
    ("default_modules/celerp-ai/celerp_ai/ui_routes.py", "_job_button"): _PAGE,
    ("default_modules/celerp-ai/celerp_ai/ui_routes.py", "_resend"): _PAGE,
    ("default_modules/celerp-ai/celerp_ai/ui_routes.py", "_showcase_view"): _PAGE,
    ("default_modules/celerp-backup/celerp_backup/routes.py", "_confirm_without_safety"): _PAGE,
    ("default_modules/celerp-docs/celerp_docs/doc_service.py", "_woocommerce_commercial_fingerprint"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/doc_service.py", "woocommerce_reconciliation_signature"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/import_service.py", "_upsert_doc"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/received.py", "document_digest"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/routes.py", "_scan_run_fingerprint"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/routes.py", "batch_import_lists"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/routes.py", "outcome_binding"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/routes.py", "preview_binding"): _DIGEST,
    ("default_modules/celerp-docs/celerp_docs/routes_share.py", "download_share_bundle"): _WIRE,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/operations.py", "_hash"): _DIGEST,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/ui_imports.py", "_operation_key"): _DIGEST,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/ui_routes.py", "_page"): _PAGE,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/ui_routes.py", "_revalue_files_card"): _PAGE,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/ui_routes.py", "_settings_fields"): _PAGE,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/ui_routes.py", "button"): _PAGE,
    ("default_modules/celerp-fixed-assets/celerp_fixed_assets/ui_upkeep.py", "_reminder_actions"): _PAGE,
    ("default_modules/celerp-inventory/celerp_inventory/routes.py", "_merge_fingerprint"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/routes.py", "_merge_request_digest"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/routes.py", "_split_request_digest"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/services.py", "_connector_event_idem"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/services.py", "build_import_records"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/services.py", "import_operation_key"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/services.py", "import_preview_hash"): _DIGEST,
    ("default_modules/celerp-inventory/celerp_inventory/services.py", "write_import_batch"): _DIGEST,
    ("default_modules/celerp-labels/celerp_labels/ui_routes.py", "_build_field_options_js"): _PAGE,
    ("default_modules/celerp-manufacturing/celerp_manufacturing/movements.py", "_fingerprint"): _DIGEST,
}


def _ascii_dumps() -> set[tuple[str, str]]:
    """(file, innermost function) of every json.dumps in the app's sources (shipped_sources)
    outside json_text.py that does not pass ensure_ascii."""
    found = set()
    for rel, path in shipped_sources():
        if rel == "celerp/json_text.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                   for a in n.names if a.name == "json"}
        stack = [(tree, "<module>")]
        while stack:
            node, fn = stack.pop()
            for child in ast.iter_child_nodes(node):
                if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                        and child.func.attr == "dumps" and getattr(child.func.value, "id", None) in aliases
                        and not any(k.arg == "ensure_ascii" for k in child.keywords)):
                    found.add((rel, fn))
                inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                stack.append((child, inner))
    return found


def test_every_json_dumps_writes_utf8_or_says_why_it_need_not():
    """A json.dumps that keeps its \\u escapes is listed with the reason they never reach the
    database; a new one fails here until it passes ensure_ascii=False, goes through dump_json,
    or is listed."""
    found = _ascii_dumps()
    assert not found - _ASCII_DUMPS.keys(), sorted(found - _ASCII_DUMPS.keys())
    assert not _ASCII_DUMPS.keys() - found, sorted(_ASCII_DUMPS.keys() - found)
