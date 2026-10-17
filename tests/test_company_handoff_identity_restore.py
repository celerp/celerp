# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company copy opened from its link keeps the sending company's record identities, so
the client company's later changes reach the records the copy already holds. This holds
on every way a copy can be opened: by a signed-in owner who already has a company, on a
first install, and by a login that has no company."""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from company_backup_support import company, confirm, download, owner, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir, _fresh_install  # noqa: F401

pytestmark = pytest.mark.asyncio

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_identity_restore", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

COPY_ID = str(uuid.UUID("2f0c5d1e-8a4b-4c3d-9e2f-1a2b3c4d5e6f"))
PROOF = "ab" * 32


@pytest.fixture(autouse=True)
def _link_verified(monkeypatch):
    """The relay confirms the uploaded file is the copy the link names."""
    async def verified(copy_id, proof, sha256):
        assert (copy_id, proof) == (COPY_ID, PROOF) and sha256
    monkeypatch.setattr("celerp.services.company_handoff_transfer.verify_company_handoff", verified)


def _link() -> dict:
    return {"handoff_copy_id": COPY_ID, "handoff_proof": PROOF}


async def _client_company(client, engine) -> tuple[bytes, str, dict[str, set[str]]]:
    """The demo company's copy, its id, and its record ids by kind."""
    registered = await client.post("/auth/register", json={
        "company_name": "Demo Trading", "email": "demo-owner@example.test",
        "name": "Demo Owner", "password": "demopw1234"})
    assert registered.status_code == 200, registered.text
    tok = registered.json()["access_token"]
    await seed_demo.seed_items(client, tok)
    contacts = await seed_demo.seed_contacts(client, tok)
    await seed_demo.seed_docs(client, tok, contacts, "invoice", 2)
    from celerp.models.projections import Projection
    from celerp.services.auth import decode_access_token
    source = str(decode_access_token(tok)["company_id"])
    async with maker(engine)() as s:
        rows = (await s.execute(select(Projection.entity_type, Projection.entity_id).where(
            Projection.company_id == uuid.UUID(source),
            Projection.entity_type.in_(("contact", "item", "doc"))))).all()
    ids: dict[str, set[str]] = {}
    for kind, entity in rows:
        ids.setdefault(kind, set()).add(entity)
    assert set(ids) == {"contact", "item", "doc"}
    return await download(client, tok), source, ids


async def _identities(engine, company_id: str, source: str) -> dict[str, set[str]]:
    from celerp.models.company_peer import CompanyPeerIdentity
    async with maker(engine)() as s:
        rows = (await s.execute(select(CompanyPeerIdentity.entity_type, CompanyPeerIdentity.source_entity_id).where(
            CompanyPeerIdentity.company_id == uuid.UUID(company_id),
            CompanyPeerIdentity.source_company_id == uuid.UUID(source)))).all()
    out: dict[str, set[str]] = {}
    for kind, entity in rows:
        out.setdefault(kind, set()).add(entity)
    return out


def _kept(identities: dict[str, set[str]], ids: dict[str, set[str]]) -> None:
    for kind, entities in ids.items():
        assert entities <= identities.get(kind, set()), kind


async def test_owner_with_a_company_keeps_identities(real_client, real_engine):
    """RED before the change: the restore asks for a receipt that exists only once the
    restore has finished, so no identity is kept."""
    data, source, ids = await _client_company(real_client, real_engine)
    accountant = await owner(real_engine, "firm@example.test", "Firm Owner")
    own = await company(real_engine, accountant, "Firm Practice", "firm-marker")
    firm_tok = await token(real_engine, accountant, own)
    preview = await real_client.post("/company-backups/read", files={"file": ("demo.celerp-company", data)},
                                     data={"mode": "new_company", **_link()}, headers=auth(firm_tok))
    assert preview.status_code == 200, preview.text
    opened = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"),
                                    headers=auth(firm_tok))
    assert opened.status_code == 201, opened.text
    _kept(await _identities(real_engine, opened.json()["company_id"], source), ids)


async def test_first_install_keeps_identities(real_client, real_engine):
    """RED before the change: as above, on the first-run restore."""
    data, source, ids = await _client_company(real_client, real_engine)
    await _fresh_install(real_engine)
    preview = await real_client.post("/company-backups/bootstrap/read",
                                     files={"file": ("demo.celerp-company", data)}, data=_link())
    assert preview.status_code == 200, preview.text
    opened = await real_client.post("/company-backups/bootstrap/restore", json={
        "upload_token": preview.json()["upload_token"], "name": "First Owner",
        "email": "first@example.test", "password": "firstpw1234"})
    assert opened.status_code == 201, opened.text
    _kept(await _identities(real_engine, opened.json()["company_id"], source), ids)


async def test_login_without_a_company_keeps_identities(real_client, real_engine):
    """RED before the change: as above, on the restore offered to a login with no company."""
    data, source, ids = await _client_company(real_client, real_engine)
    await owner(real_engine, "firm@example.test", "Firm Owner")
    login = {"email": "firm@example.test", "password": "userpw1234"}
    preview = await real_client.post("/company-backups/start-company/read",
                                     files={"file": ("demo.celerp-company", data)}, data={**login, **_link()})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    opened = await real_client.post("/company-backups/start-company/restore", json={
        **login, "upload_token": body["upload_token"], "plan_fingerprint": body["plan_fingerprint"]})
    assert opened.status_code == 201, opened.text
    _kept(await _identities(real_engine, opened.json()["company_id"], source), ids)


async def test_upload_without_a_link_keeps_no_identities(real_client, real_engine):
    """A file opened without its link is an ordinary restore: nothing ties it to the sender."""
    data, source, _ = await _client_company(real_client, real_engine)
    accountant = await owner(real_engine, "firm@example.test", "Firm Owner")
    own = await company(real_engine, accountant, "Firm Practice", "firm-marker")
    firm_tok = await token(real_engine, accountant, own)
    preview = await real_client.post("/company-backups/read", files={"file": ("demo.celerp-company", data)},
                                     data={"mode": "new_company"}, headers=auth(firm_tok))
    opened = await real_client.post("/company-backups/restore", json=confirm(preview, "new_company"),
                                    headers=auth(firm_tok))
    assert opened.status_code == 201, opened.text
    assert await _identities(real_engine, opened.json()["company_id"], source) == {}
