# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Opening a company copy where that company already exists: the preview offers to replace
it, side by side with what would be lost; only its owner replaces it, typing its name; a
backup is taken first and kept, and Undo puts the previous version back."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

import mutation_observer
from company_backup_support import download, manifest, member, owner, read, token
from migration_support import auth, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir  # noqa: F401

pytestmark = pytest.mark.asyncio

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_replace", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

NAME = "Demo Trading"


async def _demo(client) -> tuple[str, list[dict]]:
    """The demo company, registered and seeded through the API; its owner's token."""
    registered = await client.post("/auth/register", json={
        "company_name": NAME, "email": "demo-owner@example.test", "name": "Demo Owner", "password": "demopw1234"})
    assert registered.status_code == 200, registered.text
    tok = registered.json()["access_token"]
    await seed_demo.seed_items(client, tok)
    contacts = await seed_demo.seed_contacts(client, tok)
    await seed_demo.seed_docs(client, tok, contacts, "invoice", 4)
    return tok, contacts


async def _docs(engine, company_id) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(
            "SELECT count(*) FROM projections WHERE company_id = :c AND entity_type = 'doc'"),
            {"c": uuid.UUID(str(company_id))})).scalar_one()


async def _companies(engine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        return [(str(i), n) for i, n in (await conn.execute(text("SELECT id, name FROM companies ORDER BY name"))).all()]


def _cid(tok: str) -> str:
    from jose import jwt
    return jwt.get_unverified_claims(tok)["company_id"]


async def _copy_then_more_work(client, engine):
    """A copy of the demo company, then two purchase orders entered after it was made."""
    tok, contacts = await _demo(client)
    data = await download(client, tok)
    await seed_demo.seed_docs(client, tok, contacts, "purchase_order", 2)
    return tok, data


async def test_copy_of_the_same_company_offers_replace_with_what_would_be_lost(real_client, real_engine):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    cid = _cid(tok)
    preview = await read(real_client, tok, data)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    replace = body["replace"]
    assert replace["company_id"] == cid and replace["company_name"] == NAME and replace["can_replace"] is True
    current, incoming = replace["current"], replace["incoming"]
    assert current["documents"] == await _docs(real_engine, cid)
    assert incoming["documents"] == current["documents"] - 2
    assert incoming["items"] == current["items"] > 0
    assert current["last_changed"] > incoming["last_changed"]
    # The copy is older than the data here: the two purchase orders entered since would be lost.
    assert replace["older"]["documents"] == 2 and replace["older"]["since"] == incoming["last_changed"]
    assert replace["changed_after_copy"] >= 2
    # Opening it as a separate company stays available, under a name of its own.
    assert body["action"] == "create" and body["destination_name"] == f"{NAME} (Restored)"


async def test_copy_of_another_company_opens_as_new_with_no_question(real_client, real_engine):
    tok, _ = await _demo(real_client)
    data = await download(real_client, tok)
    other = await owner(real_engine, "second@example.test", "Second")
    from company_backup_support import company
    theirs = await company(real_engine, other, "Beta Goods", "beta-marker")
    preview = await read(real_client, await token(real_engine, other, theirs), data, mode="new_company")
    assert preview.status_code == 200, preview.text
    assert preview.json()["replace"] is None


async def test_import_copy_into_populated_company(real_client, real_engine):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    old = _cid(tok)
    before_docs = await _docs(real_engine, old)
    preview = (await read(real_client, tok, data)).json()
    r = await real_client.post("/company-backups/replace", json={
        "upload_token": preview["upload_token"], "mode": "settings",
        "plan_fingerprint": preview["plan_fingerprint"], "company_name": NAME}, headers=auth(tok))
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["outcome"] == "replaced" and done["company_name"] == NAME
    new = done["company_id"]
    assert new != old and (old, NAME) not in await _companies(real_engine)
    assert [n for _, n in await _companies(real_engine)] == [NAME]
    assert await _docs(real_engine, new) == before_docs - 2
    assert done["backup_created_at"] == manifest(data)["created_at"]

    # The backup taken first is kept with the company's backups.
    kept = await real_client.get("/company-backups/kept", headers=auth(done["access_token"]))
    assert kept.status_code == 200, kept.text
    [entry] = kept.json()["items"]
    assert entry["id"] == done["kept_backup_id"] and entry["company_name"] == NAME
    got = await real_client.get(f"/company-backups/kept/{entry['id']}/download", headers=auth(done["access_token"]))
    assert got.status_code == 200 and manifest(got.content)["company"]["id"] == old

    # Undo needs the same typed name, then restores the previous version in one step.
    wrong = await real_client.post(f"/company-backups/kept/{entry['id']}/replace", json={"company_name": "demo"},
                                   headers=auth(done["access_token"]))
    assert wrong.status_code == 422 and "Nothing was replaced." in wrong.json()["detail"]
    undone = await real_client.post(f"/company-backups/kept/{entry['id']}/replace", json={"company_name": NAME},
                                    headers=auth(done["access_token"]))
    assert undone.status_code == 200, undone.text
    assert [n for _, n in await _companies(real_engine)] == [NAME]
    assert await _docs(real_engine, undone.json()["company_id"]) == before_docs


async def test_replace_needs_the_typed_name_and_the_owner(real_client, real_engine):
    tok, data = await _copy_then_more_work(real_client, real_engine)
    cid = _cid(tok)
    preview = (await read(real_client, tok, data)).json()
    request = {"upload_token": preview["upload_token"], "mode": "settings",
               "plan_fingerprint": preview["plan_fingerprint"]}
    for typed in ("", "Demo trading", "Demo Trading "):
        r = await real_client.post("/company-backups/replace", json={**request, "company_name": typed},
                                   headers=auth(tok))
        assert r.status_code == 422, (typed, r.text)
        assert "Nothing was replaced." in r.json()["detail"]
    assert await _companies(real_engine) == [(cid, NAME)]

    # An admin of Demo Trading who owns a company of their own opens the copy from there.
    from company_backup_support import company
    admin = await owner(real_engine, "admin@example.test", "Admin")
    await member(real_engine, admin, uuid.UUID(cid), role="admin")
    own = await company(real_engine, admin, "Admin Books", "admin-marker")
    admin_tok = await token(real_engine, admin, own)
    theirs = (await read(real_client, admin_tok, data, mode="new_company")).json()
    assert theirs["replace"]["can_replace"] is False
    r = await real_client.post("/company-backups/replace", json={
        "upload_token": theirs["upload_token"], "mode": "new_company",
        "plan_fingerprint": theirs["plan_fingerprint"], "company_name": NAME}, headers=auth(admin_tok))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == f"Only the owner of {NAME} can replace it."
    assert (cid, NAME) in await _companies(real_engine)


async def test_failed_backup_replaces_nothing(real_client, real_engine, monkeypatch):
    from celerp.services import company_backup as cb
    tok, data = await _copy_then_more_work(real_client, real_engine)
    cid = _cid(tok)
    preview = (await read(real_client, tok, data)).json()

    async def broken(*a, **k):
        raise cb.BackupError(409, "This company has an attachment file Celerp cannot read: x.png.")

    monkeypatch.setattr(cb, "export_company_snapshot", broken)
    r = await real_client.post("/company-backups/replace", json={
        "upload_token": preview["upload_token"], "mode": "settings",
        "plan_fingerprint": preview["plan_fingerprint"], "company_name": NAME}, headers=auth(tok))
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "could not back up" in detail and "x.png" in detail and "Nothing was replaced." in detail
    assert await _companies(real_engine) == [(cid, NAME)]


async def test_a_copy_of_an_opened_copy_is_the_same_company(real_client, real_engine):
    """A company opened from a copy keeps the original's lineage, never its company id, so
    the copy it sends back is recognised where the original lives."""
    tok, _ = await _demo(real_client)
    data = await download(real_client, tok)
    preview = (await read(real_client, tok, data)).json()
    opened = await real_client.post("/company-backups/restore", json={
        "upload_token": preview["upload_token"], "mode": "settings",
        "plan_fingerprint": preview["plan_fingerprint"]}, headers=auth(tok))
    assert opened.status_code == 201, opened.text
    back = await download(real_client, opened.json()["access_token"])
    assert manifest(back)["company"]["lineage_id"] == manifest(data)["company"]["lineage_id"] != _cid(tok)
    again = await read(real_client, tok, back)
    assert again.status_code == 200, again.text
    replace = again.json()["replace"]
    assert replace["company_id"] in (_cid(tok), _cid(opened.json()["access_token"]))


# ── Changes made while the backup is kept; the follow-up after the replace commits ──

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _replace_request(preview: dict) -> dict:
    return {"upload_token": preview["upload_token"], "mode": "settings",
            "plan_fingerprint": preview["plan_fingerprint"], "company_name": NAME}


async def _with_photo(client, tok: str) -> str:
    """Store a photo for the company and link it from a location; its URL."""
    from celerp.services import attachments
    url = await attachments.store_company_file(_cid(tok), f"{uuid.uuid4().hex}.png", _PNG)
    r = await client.post("/companies/me/locations", json={"name": "Shelf", "type": "warehouse", "address": {"photo": url}},
                          headers=auth(tok))
    assert r.status_code == 200, r.text
    return url


async def _locations(engine, company_id) -> list[str]:
    async with engine.connect() as conn:
        return sorted((await conn.execute(text("SELECT name FROM locations WHERE company_id = :c"),
                                          {"c": uuid.UUID(str(company_id))})).scalars())


@pytest.mark.parametrize("change", ["location", "other_table", "attachment"])
async def test_a_change_made_after_the_backup_is_kept_stops_the_replace(real_client, real_engine, _data_dir,
                                                                       monkeypatch, change):
    """Whatever lands between the backup kept of the company and the replace's lock (a new
    location, an edited record of another table, an attachment file rewritten) is never
    deleted without being in the kept backup: the replace stops and nothing is replaced."""
    from celerp.services import attachments
    from celerp.services import company_backup as cb
    tok, contacts = await _demo(real_client)
    cid = _cid(tok)
    url = await _with_photo(real_client, tok)
    data = await download(real_client, tok)
    preview = (await read(real_client, tok, data)).json()
    keep = cb._keep_backup

    async def keep_then_change(company_id, name):
        kept = await keep(company_id, name)
        if change == "location":
            r = await real_client.post("/companies/me/locations", json={"name": "Back room", "type": "warehouse"},
                                       headers=auth(tok))
            assert r.status_code == 200, r.text
        elif change == "other_table":
            with mutation_observer.another_client():
                async with real_engine.begin() as conn:
                    await conn.execute(text(
                        "UPDATE projections SET state = CAST(CAST(state AS jsonb) || '{\"name\": \"Renamed\"}' "
                        "AS json) WHERE company_id = :c AND entity_id = (SELECT min(entity_id) FROM projections "
                        "WHERE company_id = :c AND entity_type = 'item')"), {"c": uuid.UUID(cid)})
        else:
            name = attachments.company_file_name(cid, url)
            (_data_dir / "static" / "attachments" / cid / name).write_bytes(_PNG + b"rewritten")
        return kept

    monkeypatch.setattr(cb, "_keep_backup", keep_then_change)
    r = await real_client.post("/company-backups/replace", json=_replace_request(preview), headers=auth(tok))
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "changed" and "Nothing was replaced." in r.json()["detail"]
    assert await _companies(real_engine) == [(cid, NAME)]
    assert ("Back room" in await _locations(real_engine, cid)) is (change == "location")
    assert await cb.kept_backups(cid) == []


async def test_records_checked_for_the_replace_cannot_change_until_it_commits(real_client, real_engine, _data_dir,
                                                                              monkeypatch):
    """Once the replace has compared the company with its kept backup, an edit of one of
    its records waits for the replace instead of being deleted unseen."""
    from sqlalchemy.exc import DBAPIError

    from celerp.services import company_backup as cb
    tok, data = await _copy_then_more_work(real_client, real_engine)
    cid = _cid(tok)
    preview = (await read(real_client, tok, data)).json()
    reset = cb._reset_replaced
    edits: list[str] = []

    async def edit_then_reset(session, company, *args):
        async with real_engine.connect() as conn:
            await conn.execute(text("SET lock_timeout = '300ms'"))
            try:
                await conn.execute(text(
                    "UPDATE projections SET version = version + 1 WHERE company_id = :c AND entity_id = "
                    "(SELECT min(entity_id) FROM projections WHERE company_id = :c AND entity_type = 'item')"),
                    {"c": uuid.UUID(cid)})
                edits.append("committed")
            except DBAPIError:
                edits.append("waited")
            await conn.rollback()
        return await reset(session, company, *args)

    monkeypatch.setattr(cb, "_reset_replaced", edit_then_reset)
    r = await real_client.post("/company-backups/replace", json=_replace_request(preview), headers=auth(tok))
    assert r.status_code == 200, r.text
    assert edits == ["waited"]


@pytest.mark.parametrize("step", ["settle", "load_set", "landing"])
async def test_a_failure_after_the_replace_committed_keeps_the_backup(real_client, real_engine, _data_dir,
                                                                      monkeypatch, step):
    """The replace committed, then a follow-up step failed: the company is replaced, the
    backup kept of it stays in Saved copies, the owner is told so, and trying again
    finishes the follow-up and opens the new company."""
    from celerp.modules import registry
    from celerp.services import attachments
    from celerp.services import company_backup as cb
    tok, data = await _copy_then_more_work(real_client, real_engine)
    old = _cid(tok)
    await _with_photo(real_client, tok)
    data = await download(real_client, tok)
    preview = (await read(real_client, tok, data)).json()
    broken = {"settle": (cb, "_settle_replaced"), "load_set": (cb, "commit_with_load_set"),
              "landing": (attachments, "clear_landing")}[step]
    real = getattr(*broken)
    calls: list[int] = []

    def fail_once(*a, **k):
        if not calls:
            calls.append(1)
            raise RuntimeError("follow-up failed")
        return real(*a, **k)

    monkeypatch.setattr(*broken, fail_once)
    finished: list[int] = []
    load_set = registry.commit_with_load_set

    async def counted(session):
        finished.append(1)
        return await load_set(session)

    r = await real_client.post("/company-backups/replace", json=_replace_request(preview), headers=auth(tok))
    assert r.status_code == 500, r.text
    assert r.json()["code"] == "replaced_unfinished"
    assert "Saved copies" in r.json()["detail"] and "Nothing was replaced" not in r.json()["detail"]
    [(new, name)] = await _companies(real_engine)
    assert new != old and name == NAME
    [kept] = await cb.kept_backups(new)
    assert kept["company_name"] == NAME

    monkeypatch.setattr(cb, "commit_with_load_set", counted)
    assert _cid(r.json()["access_token"]) == new  # the owner is switched to the replaced company
    again = await real_client.post("/company-backups/replace", json=_replace_request(preview),
                                   headers=auth(r.json()["access_token"]))
    assert again.status_code == 200, again.text
    assert again.json()["company_id"] == new and finished
    assert attachments.landing_companies() == []
    assert [k["id"] for k in await cb.kept_backups(new)] == [kept["id"]]


async def test_a_cancellation_after_the_replace_committed_keeps_the_backup(real_client, real_engine, _data_dir,
                                                                          monkeypatch):
    from celerp.services import company_backup as cb
    tok, data = await _copy_then_more_work(real_client, real_engine)
    old = _cid(tok)
    preview = (await read(real_client, tok, data)).json()
    from jose import jwt

    from celerp.services import company_backup_files as files
    user_id = jwt.get_unverified_claims(tok)["sub"]
    stage = files.stage_path(user_id, preview["upload_token"])

    async def cancelled(session):
        raise asyncio.CancelledError

    monkeypatch.setattr(cb, "commit_with_load_set", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await cb.replace_company(stage, mode="settings", user_id=user_id, current_company_id=old,
                                 plan_fingerprint=preview["plan_fingerprint"], typed_name=NAME)
    [(new, _)] = await _companies(real_engine)
    assert new != old
    [kept] = await cb.kept_backups(new)
    assert kept["company_name"] == NAME


async def test_turning_a_module_on_while_the_company_is_replaced_waits_for_the_replace(real_client, real_engine,
                                                                                    monkeypatch):
    """A module is turned on for the company while its replace holds the company: the turn-on
    waits for the replace, never a deadlock or a server error, then is told the access it
    was saving under changed; the replace goes ahead."""
    from celerp.services import company_backup as cb
    tok, data = await _copy_then_more_work(real_client, real_engine)
    preview = (await read(real_client, tok, data)).json()
    held, release = asyncio.Event(), asyncio.Event()
    lock = cb.lock_company_for_deletion

    async def lock_then_hold(session, company_id):
        found = await lock(session, company_id)
        if not held.is_set():
            held.set()
            await release.wait()
        return found

    monkeypatch.setattr(cb, "lock_company_for_deletion", lock_then_hold)
    replacing = asyncio.create_task(real_client.post("/company-backups/replace", json=_replace_request(preview),
                                                     headers=auth(tok)))
    await asyncio.wait_for(held.wait(), 60)
    turning_on = asyncio.create_task(real_client.post("/companies/me/modules/celerp-docs/enable", headers=auth(tok)))
    for _ in range(400):
        async with real_engine.connect() as conn:
            if (await conn.execute(text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock'"))).scalar_one():
                break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("the turn-on never waited for the replace")
    release.set()
    replaced = await asyncio.wait_for(replacing, 60)
    turned_on = await asyncio.wait_for(turning_on, 60)
    assert replaced.status_code == 200, replaced.text
    assert turned_on.status_code == 403, turned_on.text
    assert "access to this company was changed" in turned_on.json()["detail"]
    assert [n for _, n in await _companies(real_engine)] == [NAME]
