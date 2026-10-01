# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company backup is checked as business data, not only as a file: numbers compare by
value however they are spelled, a restore whose records disagree with the history they
come from is refused, a preview holds only for the exact file it was made from, and who
made each change travels with the history without bringing any user account along."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import (
    confirm,
    download,
    members,
    owner,
    read,
    restore,
    rezip,
    settle,
    snapshot,
    token,
)
from migration_support import auth, count, real_client, real_engine  # noqa: F401
from test_company_backup import (
    _bk_seed_portable,
    _r_alter_rows,
    _r_created,
    _r_env,
    _r_refused,
    _r_rows,
    _r_source,
    _r_table_lines,
    _r_with_rows,
)

pytestmark = pytest.mark.asyncio


def _cb():
    from celerp.services import company_backup
    return company_backup


def _ref(user_id) -> str:
    return hashlib.sha256(f"celerp-backup-actor:{user_id}".encode()).hexdigest()


def _staged_file(tmp_path, upload_token: str):
    (path,) = (tmp_path / "company_backups" / "uploads").glob(f"*-{upload_token}.celerp-company")
    return path


async def _ledger_events(engine, cid, n: int, actor) -> None:
    """n more events by ``actor``, and the records they produce."""
    async with engine.begin() as conn:
        for i in range(n):
            await conn.execute(text(
                "INSERT INTO ledger (company_id, entity_id, entity_type, event_type, data, actor_id, source, "
                "idempotency_key) VALUES (:c, :e, 'item', 'item.created', CAST(:d AS json), :u, 'api', :k)"),
                {"c": cid, "e": f"item:a{i}", "d": json.dumps({"name": f"event-{i}"}), "u": actor, "k": f"a-{i}"})
    await settle(engine, cid)


# ── Numbers compare by value ─────────────────────────────────────────────────

def test_row_digest_compares_numbers_by_value():
    """0.00, 0.0, 0 and -0.0 are one value; 1.10 and 1.1 are one value; 1.1 and 1.2 are not;
    text, booleans and null stay what they are."""
    cb = _cb()
    num = lambda s: "\x00" + s + "\x00"  # noqa: E731 - a number as _parse_row keeps it
    same = [({"a": num("0.00")}, {"a": num("0.0")}), ({"a": num("0.0")}, {"a": 0}),
            ({"a": num("-0.0")}, {"a": 0}), ({"a": [num("1.10")]}, {"a": [num("1.1")]}),
            ({"a": {"b": num("100.0")}}, {"a": {"b": 100}}), ({"a": num("1e2")}, {"a": 100})]
    for left, right in same:
        assert cb._row_digest(left) == cb._row_digest(right), (left, right)
    different = [({"a": num("1.1")}, {"a": num("1.2")}), ({"a": "1"}, {"a": 1}), ({"a": True}, {"a": 1}),
                 ({"a": None}, {"a": 0}), ({"a": num("12345678901234567890.1")}, {"a": num("12345678901234567890.2")})]
    for left, right in different:
        assert cb._row_digest(left) != cb._row_digest(right), (left, right)


async def test_rewritten_number_spelling_restores(real_engine, real_client, tmp_path, monkeypatch):
    """A backup whose numbers were rewritten without changing their value (100.00 written as
    100.0) restores, and the restored amounts are the source's."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    await _bk_seed_portable(real_engine, cid, "alpha-marker")
    data = await download(real_client, tok)
    assert b'"statement_balance": 100.00' in b"\n".join(_r_table_lines(data, "reconciliation_sessions"))
    rewritten = _r_with_rows(data, "reconciliation_sessions", lambda row: row)
    assert b'"statement_balance": 100.0,' in b"\n".join(_r_table_lines(rewritten, "reconciliation_sessions"))
    body = _r_created(await restore(real_client, tok, rewritten, mode="new_company"))
    (session,) = await _r_rows(real_engine, "reconciliation_sessions", uuid.UUID(body["company_id"]))
    assert str(session["statement_balance"]) in ("100.00", "100.0", "100")
    assert float(session["statement_balance"]) == 100


async def test_read_back_refusal_names_no_table(real_engine, real_client, tmp_path, monkeypatch):
    """A restore whose records do not read back as the backup holds them is refused in plain
    words, without naming a database table."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    before = await snapshot(real_engine)
    async with _r_alter_rows(real_engine):
        r = await restore(real_client, tok, data, mode="new_company")
    _r_refused(r)
    assert r.json()["detail"] == _cb().MISMATCH
    assert "projections" not in r.json()["detail"] and "(" not in r.json()["detail"]
    assert await snapshot(real_engine) == before


# ── Records must agree with their history ────────────────────────────────────

async def test_record_disagreeing_with_history_refused(real_engine, real_client, tmp_path, monkeypatch):
    """A backup whose record was edited (with its hashes made to match) so it no longer
    agrees with the events that produced it is refused, and nothing is written."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    forged = _r_with_rows(data, "projections",
                          lambda row: {**row, "state": {**row["state"], "name": "forged total"}})
    before = await snapshot(real_engine)
    r = await restore(real_client, tok, forged, mode="new_company")
    _r_refused(r)
    assert r.json()["detail"] == _cb().DISAGREE
    assert await snapshot(real_engine) == before
    assert await count(real_engine, "companies") == 1


@pytest.mark.parametrize("emptied", ["ledger", "projections"])
async def test_record_without_its_history_refused(real_engine, real_client, tmp_path, monkeypatch, emptied):
    """Records with no history behind them, or history with its records missing, are refused."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    parts = members(data)
    meta = json.loads(parts["manifest.json"])
    parts[f"tables/{emptied}.jsonl"] = b""
    meta["tables"][emptied].update(rows=0, sha256=hashlib.sha256(b"").hexdigest())
    parts["manifest.json"] = json.dumps(meta).encode()
    before = await snapshot(real_engine)
    r = await restore(real_client, tok, rezip(parts), mode="new_company")
    _r_refused(r)
    assert r.json()["detail"] == _cb().DISAGREE
    assert await snapshot(real_engine) == before


async def test_restored_records_keep_their_own_times(real_engine, real_client, tmp_path, monkeypatch):
    """Checking the records against their history leaves the restored records exactly as
    the backup holds them, times and versions included."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    (source,) = await _r_rows(real_engine, "projections", cid)
    (restored,) = await _r_rows(real_engine, "projections", uuid.UUID(body["company_id"]))
    for column in ("created_at", "updated_at", "version", "state"):
        if column == "state":
            assert set(restored[column]) == set(source[column])
        else:
            assert restored[column] == source[column], column


# ── A preview holds for the exact file ───────────────────────────────────────

async def test_file_replaced_after_preview_is_stale(real_engine, real_client, tmp_path, monkeypatch):
    """Confirming a preview after its uploaded file was replaced by different bytes of the
    same backup writes nothing and asks for a fresh preview."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    preview = await read(real_client, tok, data, mode="new_company")
    assert preview.status_code == 200, preview.text
    staged = _staged_file(tmp_path, preview.json()["upload_token"])
    staged.write_bytes(_r_with_rows(data, "ledger", lambda row: row))
    before = await snapshot(real_engine)
    r = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"), headers=auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "stale_preview"
    assert await snapshot(real_engine) == before


async def test_same_backup_with_different_contents_refused(real_engine, real_client, tmp_path, monkeypatch):
    """Once a backup is restored, a file carrying the same backup but different contents is
    refused rather than treated as that backup; the original file still opens the company."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    first = _r_created(await restore(real_client, tok, data, mode="new_company"))
    changed = _r_with_rows(data, "ledger", lambda row: {**row, "data": {**row["data"], "name": "other"}})
    before = await snapshot(real_engine)
    r = await read(real_client, tok, changed, mode="new_company")
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == _cb().CHANGED_COPY
    assert await snapshot(real_engine) == before
    again = await restore(real_client, tok, data, mode="new_company")
    assert again.status_code == 200, again.text
    assert (again.json()["company_id"], again.json()["created"]) == (first["company_id"], False)


async def test_company_restored_before_file_hash_kept_still_opens(real_engine, real_client, tmp_path, monkeypatch):
    """A company restored before the file's hash was kept is found again by its backup, so
    restoring that backup again opens it instead of making a copy."""
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    first = _r_created(await restore(real_client, tok, data, mode="new_company"))
    async with real_engine.begin() as conn:
        await conn.execute(text(
            "UPDATE companies SET settings = CAST(CAST(settings AS jsonb) #- '{restored_backup,sha256}' AS json) "
            "WHERE id = :c"), {"c": uuid.UUID(first["company_id"])})
    again = await restore(real_client, tok, _r_with_rows(data, "ledger", lambda row: row), mode="new_company")
    assert again.status_code == 200, again.text
    assert (again.json()["company_id"], again.json()["created"]) == (first["company_id"], False)


async def test_restored_company_records_file_hash(real_engine, real_client, tmp_path, monkeypatch):
    _r_env(tmp_path, monkeypatch)
    _, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    async with real_engine.connect() as conn:
        kept = (await conn.execute(text(
            "SELECT settings -> 'restored_backup' ->> 'sha256' FROM companies WHERE id = :c"),
            {"c": uuid.UUID(body["company_id"])})).scalar()
    assert kept == hashlib.sha256(data).hexdigest()


# ── Who made each change ─────────────────────────────────────────────────────

async def test_backup_carries_author_label_for_every_event(real_engine, real_client, tmp_path, monkeypatch):
    """Every event with an author carries that author's name and a one-way reference to
    their account, never the account's id, email or password."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    await _ledger_events(real_engine, cid, 146, user)
    data = await download(real_client, tok)
    rows = [json.loads(line) for line in _r_table_lines(data, "ledger")]
    assert len(rows) == 147
    for row in rows:
        assert row["actor_id"] is None
        assert row["metadata"]["backup_actor"] == {"name": "Owner", "user_ref": _ref(user)}
    body = b"".join(members(data).values())
    assert str(user).encode() not in body


async def test_same_company_restore_relinks_exact_authors(real_engine, real_client, tmp_path, monkeypatch):
    """A Settings restore of the same company links each event back to the exact user who
    made it, and gives no one a membership because they appear in the history."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    clerk = await owner(real_engine, email="clerk@example.com", name="Clerk")
    await _ledger_events(real_engine, cid, 2, clerk)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data))
    new = uuid.UUID(body["company_id"])
    actors = sorted(str(row["actor_id"]) for row in await _r_rows(real_engine, "ledger", new))
    assert actors == sorted([str(user), str(clerk), str(clerk)])
    assert await count(real_engine, "user_companies", "company_id = :c AND user_id = :u", c=new, u=clerk) == 0


async def test_other_company_restore_keeps_names_without_linking_users(real_engine, real_client, tmp_path,
                                                                       monkeypatch):
    """Restored as a separate company, history links to no user, and the activity list shows
    each author's name marked as coming from a backup."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    new = uuid.UUID(body["company_id"])
    assert [row["actor_id"] for row in await _r_rows(real_engine, "ledger", new)] == [None]
    new_tok = await token(real_engine, user, new)
    r = await real_client.get("/ledger", params={"resolve": "true"}, headers=auth(new_tok))
    assert r.status_code == 200, r.text
    (item,) = r.json()["items"]
    assert (item["actor_name"], item["actor_historical"]) == ("Owner", True)


async def test_same_name_never_links_an_author(real_engine, real_client, tmp_path, monkeypatch):
    """Another user with the author's name, here or elsewhere, is never linked to the history."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    namesake = await owner(real_engine, email="namesake@example.com", name="Owner")
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    rows = await _r_rows(real_engine, "ledger", uuid.UUID(body["company_id"]))
    assert [row["actor_id"] for row in rows] == [None]
    assert str(namesake) not in json.dumps(rows)


async def test_author_unknown_here_keeps_readable_label(real_engine, real_client, tmp_path, monkeypatch):
    """An author who is not a user of this installation is left unlinked, with their name kept."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    elsewhere = _r_with_rows(data, "ledger", lambda row: {**row, "metadata": {
        "backup_actor": {"name": "Former clerk", "user_ref": _ref(uuid.uuid4())}}})
    body = _r_created(await restore(real_client, tok, elsewhere))
    (row,) = await _r_rows(real_engine, "ledger", uuid.UUID(body["company_id"]))
    assert row["actor_id"] is None
    assert row["metadata"]["backup_actor"]["name"] == "Former clerk"


async def test_backup_of_restored_company_keeps_author_labels(real_engine, real_client, tmp_path, monkeypatch):
    """Backing up a company restored from another backup carries the original authors on."""
    _r_env(tmp_path, monkeypatch)
    user, cid, tok = await _r_source(real_engine)
    data = await download(real_client, tok)
    body = _r_created(await restore(real_client, tok, data, mode="new_company"))
    again = await download(real_client, await token(real_engine, user, uuid.UUID(body["company_id"])))
    (row,) = [json.loads(line) for line in _r_table_lines(again, "ledger")]
    assert row["metadata"]["backup_actor"] == {"name": "Owner", "user_ref": _ref(user)}


def test_actor_label_marks_names_from_a_backup():
    from ui.components.activity import actor_label
    assert actor_label({"actor_name": "Owner"}) == "Owner"
    assert actor_label({"actor_name": "Owner", "actor_historical": True}) == "Owner (from a backup)"
    assert actor_label({"actor_name": str(uuid.uuid4())}) == "--"
    assert actor_label({}) == "--"
