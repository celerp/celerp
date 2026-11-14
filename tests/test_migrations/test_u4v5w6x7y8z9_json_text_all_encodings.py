# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every json value an earlier version stored with characters past ASCII as \\u escapes is
rewritten as UTF-8 text on every encoding, so a UTF8 install's backup restores into a SQL_ASCII
one. A row that already holds an escape PostgreSQL cannot turn into text (a lone surrogate, NUL)
keeps it byte for byte: migrations write existing rows back through dump_existing_json, so such
a row neither stops the upgrade nor reads differently after it."""

from __future__ import annotations

import ast
import asyncio
import json

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from celerp.json_text import SYNC_TEXT, dump_json
from company_backup_support import member, owner, token
from migration_support import auth, real_client  # noqa: F401
from shipped_sources import shipped_sources
from .conftest import head_rev, run_migration_ops, throwaway_db, upgrade_to
from .test_r1s2t3u4v5w6_projections_canonical_number_lookup import ENCODINGS
from .test_s2t3u4v5w6x7_json_text_utf8 import BEFORE_STATE_INDEX, RELEASED_HEAD, THAI, _sql

REVISION = "u4v5w6x7y8z9_json_text_unescape_all_encodings"
EMOJI = "\U0001F600"
LONE = "\ud83d"
# Company settings as an earlier version stored them (json.dumps writes every character past
# ASCII as an escape, a lone surrogate included): one escape json can turn into text, one NUL
# escape it cannot, one lone surrogate it cannot, and Thai beside a lone surrogate.
NOTES = {"thai": {"title": THAI}, "nul": {"title": "a\x00b"}, "lone": {"title": LONE},
         "mixed": {"title": THAI, "body": LONE}}


def _note_rows(sync_url: str, cid: str) -> None:
    """One company per NOTES entry, its slug the key: json outside projections, which no index reads."""
    for key, value in NOTES.items():
        _sql(sync_url, "INSERT INTO companies (id, name, slug, settings, created_at) VALUES "
                       "(gen_random_uuid(), :k, :k, CAST(:s AS json), now())", {"k": key, "s": json.dumps(value)})


def _company(sync_url: str) -> str:
    return str(_sql(sync_url, "INSERT INTO companies (id, name, slug, settings, created_at) VALUES "
                              "(gen_random_uuid(), 'Demo', 'demo', CAST(:s AS json), now()) RETURNING id",
                    {"s": json.dumps({"company_address": THAI})})[0][0])


def _stored(sync_url: str) -> dict[str, str]:
    return dict(_sql(sync_url, "SELECT slug, CAST(settings AS text) FROM companies WHERE slug = ANY(:k)",
                     {"k": list(NOTES)}))


def _arrow(sync_url: str) -> dict[str, tuple]:
    """What ->> 'title' gives on each NOTES company: its text, or the first line of PostgreSQL's refusal."""
    out = {}
    for key in NOTES:
        try:
            out[key] = ("ok", _sql(sync_url, "SELECT settings ->> 'title' FROM companies WHERE slug = :k",
                                   {"k": key})[0][0])
        except sa.exc.DBAPIError as exc:
            out[key] = ("refused", str(exc.orig).splitlines()[0])
    return out


def _migrate(async_url: str) -> None:
    """Upgrade to head and replay the data backfills, as `celerp migrate` and a restore's reconcile run them."""
    from celerp.cli import _apply_migrations, _migration_lock, _reconcile_after_migrate

    with _migration_lock(async_url):
        _apply_migrations(async_url)
        _reconcile_after_migrate(async_url)


def test_dump_existing_json_keeps_lone_surrogates_escaped():
    from celerp.json_text import dump_existing_json

    for value in ({"t": LONE}, {"t": "\udc00"}, {"t": "\ude00\ud83d"}, {LONE: 1}, [THAI, LONE, EMOJI]):
        written = dump_existing_json(value)
        written.encode("utf-8")
        assert json.loads(written) == value
        assert "\\ud83d" in written or "\\udc00" in written or "\\ude00" in written
    for value in ({"t": EMOJI}, {"t": "a\x00b"}, {"t": THAI}, [1, None, True, "x"]):
        assert dump_existing_json(value) == dump_json(value)
    with pytest.raises(UnicodeEncodeError):
        dump_json({"t": LONE}).encode("utf-8")


def _migration_text_uses() -> set[tuple[str, str]]:
    """Every (file, innermost function) that reads MIGRATION_TEXT, outside the module defining it."""
    uses = set()

    class Census(ast.NodeVisitor):
        def __init__(self, rel):
            self.rel, self.stack = rel, ["<module>"]

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Name(self, node):
            if node.id == "MIGRATION_TEXT" and isinstance(node.ctx, ast.Load):
                uses.add((self.rel, self.stack[-1]))

    for rel, path in shipped_sources():
        if rel != "celerp/json_text.py":
            Census(rel).visit(ast.parse(path.read_text(encoding="utf-8")))
    return uses


def test_migration_engines_write_existing_rows_through_dump_existing_json(monkeypatch):
    """Each engine migration code runs on writes existing rows through dump_existing_json: env.py
    with no held fence (plain alembic), env.py under the held fence (from the released head the
    upgrade runs s2t3u4v5w6x7 and u4v5w6x7y8z9 itself), and the data-backfill replay (from
    t3u4v5w6x7y8 the stamp walks past u4v5w6x7y8z9 and the replay runs it)."""
    from celerp.json_text import MIGRATION_TEXT, dump_existing_json
    from celerp.migrations import _data_reconcile, _json_compat
    from celerp.migrations.compatibility import mutating_scope

    rewrites: list = []
    replays: list = []
    rewrite, replay = _json_compat.unescape_json_text, _data_reconcile.replay_data_backfills

    def recording_rewrite(conn):
        bound = sa.bindparam("v", {"t": LONE}, type_=sa.JSON)
        rewrites.append((conn.engine.dialect._json_serializer,
                         conn.execute(sa.select(sa.cast(bound, sa.Text))).scalar_one()))
        return rewrite(conn)

    def recording_replay(engine):
        replays.append(engine.dialect._json_serializer)
        return replay(engine)

    def taken() -> list:
        out = list(rewrites)
        rewrites.clear()
        return out

    monkeypatch.setattr(_json_compat, "unescape_json_text", recording_rewrite)
    monkeypatch.setattr(_data_reconcile, "replay_data_backfills", recording_replay)
    with throwaway_db("jsonfence", "UTF8") as (async_url, sync_url):
        upgrade_to(sync_url, RELEASED_HEAD)  # plain alembic: env.py's branch with no held fence
        no_fence = taken()
        _migrate(async_url)  # the held fence runs every revision past the released head
        held = taken()
        assert replays == [dump_existing_json], replays
    with throwaway_db("jsonreplay", "UTF8") as (async_url, sync_url):
        upgrade_to(sync_url, "t3u4v5w6x7y8")
        taken()
        _migrate(async_url)  # the stamp walks past u4v5w6x7y8z9; the replay runs it
        replayed = taken()
        with mutating_scope(sync_url) as fence, fence.engine() as other:
            other_serializer = other.dialect._json_serializer

    escaped = (dump_existing_json, '{"t": "\\ud83d"}')
    assert no_fence and set(no_fence) == {escaped}, no_fence
    assert held == [escaped, escaped], held  # s2t3u4v5w6x7, then u4v5w6x7y8z9
    assert replayed == [escaped], replayed
    assert replays == [dump_existing_json, dump_existing_json], replays
    assert other_serializer is dump_json
    assert MIGRATION_TEXT == {**SYNC_TEXT, "json_serializer": dump_existing_json}
    assert _migration_text_uses() == {("celerp/migrations/env.py", "run_migrations_online"),
                                      ("celerp/cli.py", "_reconcile_after_migrate")}


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("start", [RELEASED_HEAD, "t3u4v5w6x7y8"])
def test_u4v5w6x7y8z9_rewrites_escapes_on_every_encoding(encoding, start):
    with throwaway_db("jsonall", encoding) as (async_url, sync_url):
        upgrade_to(sync_url, start)
        cid = _company(sync_url)
        _note_rows(sync_url, cid)
        before, arrow = _stored(sync_url), _arrow(sync_url)

        _migrate(async_url)

        after = _stored(sync_url)
        assert {k: json.loads(v) for k, v in after.items()} == NOTES
        assert after["thai"] == dump_json(NOTES["thai"])
        assert after["nul"] == before["nul"] == '{"title": "a\\u0000b"}'
        assert after["lone"] == before["lone"] == '{"title": "\\ud83d"}'
        assert after["mixed"] == '{"title": "%s", "body": "\\ud83d"}' % THAI
        assert _sql(sync_url, "SELECT CAST(settings AS text) FROM companies WHERE slug = 'demo'") == [
            (dump_json({"company_address": THAI}),)]
        assert {k: arrow[k] for k in ("nul", "lone")} == {k: _arrow(sync_url)[k] for k in ("nul", "lone")}
        assert _sql(sync_url, "SELECT version_num FROM alembic_version") == [(head_rev(),)]


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_u4v5w6x7y8z9_second_run_rewrites_nothing(encoding):
    from celerp.json_text import MIGRATION_TEXT
    from celerp.migrations._json_compat import unescape_json_text

    with throwaway_db("jsonagain", encoding) as (async_url, sync_url):
        upgrade_to(sync_url, RELEASED_HEAD)
        _note_rows(sync_url, _company(sync_url))
        _migrate(async_url)
        first = _stored(sync_url)
        engine = create_engine(sync_url, **MIGRATION_TEXT)
        try:
            with engine.begin() as conn:
                assert unescape_json_text(conn) == 0
            run_migration_ops(engine, REVISION)
        finally:
            engine.dispose()
        assert _stored(sync_url) == first


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_data_helpers_write_a_lone_surrogate_row_back_escaped(encoding):
    """A data backfill that edits a row holding a lone surrogate (the update_* helpers, through
    _update_json_column) writes that escape back as it was and the edit beside it."""
    from celerp.json_text import MIGRATION_TEXT
    from celerp.migrations._json_compat import update_company_settings

    with throwaway_db("jsonhelper", encoding) as (_async_url, sync_url):
        upgrade_to(sync_url, "t3u4v5w6x7y8")
        _note_rows(sync_url, _company(sync_url))
        engine = create_engine(sync_url, **MIGRATION_TEXT)
        try:
            with engine.begin() as conn:
                assert update_company_settings(conn, lambda settings, _row: settings.update(seen=True) or True,
                                               where="slug = 'lone'") == 1
        finally:
            engine.dispose()
        assert _stored(sync_url)["lone"] == '{"title": "\\ud83d", "seen": true}'


@pytest.fixture(params=ENCODINGS)
def encoded_db(request):
    with throwaway_db("jsondocs", request.param) as urls:
        yield urls


@pytest.fixture()
def real_engine(encoded_db, monkeypatch):
    """The API's engine on the throwaway database the test migrates."""
    import celerp.db
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(encoded_db[0], poolclass=NullPool, json_serializer=dump_json)
    monkeypatch.setattr(celerp.db, "engine", engine)
    yield engine


async def test_get_docs_reads_every_row_after_u4v5w6x7y8z9(encoded_db, real_engine, real_client):  # noqa: F811
    async_url, sync_url = encoded_db
    doc = {"doc_type": "invoice", "ref_id": f"{THAI}-1", "doc_number": f"{THAI}-1", "status": "draft",
           "contact_name": f"{THAI} {EMOJI}", "line_items": [], "total": 0}
    lst = {"list_type": "price_list", "ref_id": f"{THAI}-L1", "doc_number": f"{THAI}-L1", "status": "draft",
           "contact_name": f"{THAI} {EMOJI}", "line_items": []}
    await asyncio.to_thread(upgrade_to, sync_url, BEFORE_STATE_INDEX)
    cid = _company(sync_url)
    for key, kind, state in (("doc:escaped", "doc", doc), ("list:escaped", "list", lst)):
        _sql(sync_url, "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                       "VALUES (:c, :e, :t, CAST(:s AS json), 1, now())",
             {"c": cid, "e": key, "t": kind, "s": json.dumps(state)})
    await asyncio.to_thread(upgrade_to, sync_url, RELEASED_HEAD)
    _note_rows(sync_url, cid)
    before, arrow = _stored(sync_url), _arrow(sync_url)

    await asyncio.to_thread(_migrate, async_url)
    assert _sql(sync_url, "SELECT version_num FROM alembic_version") == [(head_rev(),)]

    user = await owner(real_engine)
    await member(real_engine, user, cid, "owner")
    headers = auth(await token(real_engine, user, cid))
    docs = await real_client.get("/docs", headers=headers)
    lists = await real_client.get("/lists", headers=headers)
    assert docs.status_code == lists.status_code == 200, (docs.text, lists.text)
    got = {i["id"]: i for i in docs.json()["items"] + lists.json()["items"]}
    # The list summary shows the contact as "customer".
    for key, state, contact in (("doc:escaped", doc, "contact_name"), ("list:escaped", lst, "customer")):
        assert (got[key]["ref_id"], got[key][contact]) == (state["ref_id"], state["contact_name"])
    after = _stored(sync_url)
    assert (after["nul"], after["lone"]) == (before["nul"], before["lone"])
    assert {k: arrow[k] for k in ("nul", "lone")} == {k: _arrow(sync_url)[k] for k in ("nul", "lone")}
    assert _arrow(sync_url)["thai"] == ("ok", THAI)


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_json_rewrites_read_and_write_in_bounded_batches(encoding, monkeypatch):
    """Both json rewrites (unescape_json_text, and _update_json_column under every update_* helper)
    hold at most BATCH rows at a time, so a large install's upgrade runs in memory flat in its row
    count: with a batch of 7 over 50 escaped rows no statement returns or writes more than 7 rows,
    every row is rewritten exactly once and the helper's mutate sees each row once."""
    from sqlalchemy import event

    from celerp.json_text import MIGRATION_TEXT
    from celerp.migrations import _json_compat

    monkeypatch.setattr(_json_compat, "BATCH", 7)
    with throwaway_db("jsonbatch", encoding) as (_async_url, sync_url):
        upgrade_to(sync_url, "t3u4v5w6x7y8")
        _sql(sync_url, "INSERT INTO companies (id, name, slug, settings, created_at) SELECT gen_random_uuid(), "
                       "'c' || g, 'c' || g, CAST(replace(:s, '@', g::text) AS json), now() "
                       "FROM generate_series(1, 50) g", {"s": json.dumps({"title": f"{THAI} @"})})
        engine = create_engine(sync_url, **MIGRATION_TEXT)
        taken: list[tuple[str, int]] = []
        # Every statement but the read of which columns are json, with the rows it returned or wrote.
        event.listen(engine, "after_cursor_execute", lambda _c, cursor, statement, *_a: taken.append(
            (statement.split()[0].upper(), cursor.rowcount)) if "information_schema" not in statement else None)
        seen: list[str] = []
        phases = []
        try:
            with engine.begin() as conn:
                assert _json_compat.unescape_json_text(conn) == 50
            phases.append(list(taken))
            taken.clear()
            with engine.begin() as conn:
                assert _json_compat.update_company_settings(
                    conn, lambda settings, row: seen.append(row["slug"]) or settings.update(seen=True) or True,
                    extra_cols=("slug",)) == 50
            phases.append(list(taken))
        finally:
            engine.dispose()
        for phase in phases:
            assert max(rows for _verb, rows in phase) <= 7, phase
            assert sum(rows for verb, rows in phase if verb == "UPDATE") == 50, phase
        assert sorted(seen) == sorted(f"c{g}" for g in range(1, 51))
        assert dict(_sql(sync_url, "SELECT slug, CAST(settings AS text) FROM companies")) == {
            f"c{g}": dump_json({"title": f"{THAI} {g}", "seen": True}) for g in range(1, 51)}
