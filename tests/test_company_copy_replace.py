# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Opening a company copy where that company already exists: the preview offers to replace
it, side by side with what would be lost; only its owner replaces it, typing its name; a
backup is taken first and kept, and Undo puts the previous version back."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

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
    """A copy of the demo company, then two more invoices entered after it was made."""
    tok, contacts = await _demo(client)
    data = await download(client, tok)
    await seed_demo.seed_docs(client, tok, contacts, "invoice", 2)
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
    # The copy is older than the data here: the two invoices entered since would be lost.
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


async def test_replace_puts_the_copy_in_place_and_undo_restores_the_previous_version(real_client, real_engine):
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

    admin = await owner(real_engine, "admin@example.test", "Admin")
    await member(real_engine, admin, uuid.UUID(cid), role="admin")
    admin_tok = await token(real_engine, admin, cid, role="admin")
    theirs = (await read(real_client, admin_tok, data, mode="new_company")).json()
    assert theirs["replace"]["can_replace"] is False
    r = await real_client.post("/company-backups/replace", json={
        "upload_token": theirs["upload_token"], "mode": "new_company",
        "plan_fingerprint": theirs["plan_fingerprint"], "company_name": NAME}, headers=auth(admin_tok))
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == f"Only the owner of {NAME} can replace it."
    assert await _companies(real_engine) == [(cid, NAME)]


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
    """A company opened from a copy keeps the original's company id as its own lineage, so
    the copy it sends back is recognised where the original lives."""
    tok, _ = await _demo(real_client)
    data = await download(real_client, tok)
    preview = (await read(real_client, tok, data)).json()
    opened = await real_client.post("/company-backups/restore", json={
        "upload_token": preview["upload_token"], "mode": "settings",
        "plan_fingerprint": preview["plan_fingerprint"]}, headers=auth(tok))
    assert opened.status_code == 201, opened.text
    back = await download(real_client, opened.json()["access_token"])
    assert manifest(back)["company"]["lineage_id"] == _cid(tok)
