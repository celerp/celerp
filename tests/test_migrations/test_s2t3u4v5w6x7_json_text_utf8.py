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
import pathlib
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from celerp.json_text import SYNC_TEXT, dump_json
from .conftest import throwaway_db, upgrade_to
from .test_r1s2t3u4v5w6_projections_canonical_number_lookup import ENCODINGS

# The revision before the first expression index on projection state (ix_projections_connector_key).
BEFORE_STATE_INDEX = "c2d3e4f5a6b7"
THAI = "ใบแจ้งหนี้"
ROOT = pathlib.Path(__file__).resolve().parents[2]


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


def test_the_escape_rewrite_runs_again_without_changing_anything():
    from celerp.migrations._json_compat import unescape_json_text

    with throwaway_db("jsonidem", "SQL_ASCII") as (_, sync_url):
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


_SYNC_FACTORIES = {"create_engine", "engine_from_config"}


def _passes_sync_text(call: ast.Call) -> bool:
    return any(k.arg is None and getattr(k.value, "id", None) == "SYNC_TEXT" for k in call.keywords)


def test_every_engine_writes_json_as_utf8_text():
    """Every engine the app or a migration opens serializes JSON with dump_json, so no writer
    stores a \\u escape past ASCII; a psycopg2 engine also talks UTF-8 (SYNC_TEXT), else it could
    neither send that text to a SQL_ASCII database nor read its bytes back."""
    missing = []
    for path in [*ROOT.glob("celerp/**/*.py"), *ROOT.glob("default_modules/*/*/**/*.py")]:
        if "tests" in path.relative_to(ROOT).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = getattr(node.func, "attr", getattr(node.func, "id", None)) if isinstance(node, ast.Call) else None
            if name in _SYNC_FACTORIES and not _passes_sync_text(node) or name == "create_async_engine" and not any(
                    k.arg == "json_serializer" for k in node.keywords):
                missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not missing, missing
