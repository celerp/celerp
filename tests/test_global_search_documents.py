# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Global search finds documents and Lists by number and by the customer or supplier named on them.

Reached through the real aggregator (GET /search) with the documents provider registered under its
real dotted handler, so company scoping and the view_documents gate are the ones production uses.
"""
from __future__ import annotations

import datetime as _dt
import uuid

import pytest

from celerp.modules import slots
from celerp.models.projections import Projection
from test_helpers import invite_user, register_admin

pytestmark = pytest.mark.asyncio


@pytest.fixture
def docs_provider():
    saved = list(slots.get("search_provider"))
    slots._slots["search_provider"] = [{
        "handler": "celerp_docs.search:global_search", "result_key": "items",
        "permission": "view_documents", "_module": "celerp-docs", "_first_party": True}]
    yield
    slots._slots["search_provider"] = saved


def _company_id(token: str) -> str:
    from celerp.services.auth import get_token_claims
    return get_token_claims(token)["company_id"]


def _seed(session, company_id: str, entity_type: str, entity_id: str, state: dict) -> None:
    session.add(Projection(company_id=company_id, entity_id=entity_id, entity_type=entity_type,
                           state=state, version=1, updated_at=_dt.datetime.now(_dt.timezone.utc)))


async def _search(client, headers, q: str) -> list[dict]:
    r = await client.get("/search", params={"q": q}, headers=headers)
    assert r.status_code == 200, r.text
    return (r.json()["results"].get("celerp-docs") or {}).get("items") or []


async def test_a_document_is_found_by_its_number(client, session, docs_provider):
    tok = await register_admin(client)
    tag = uuid.uuid4().hex[:6].upper()
    _seed(session, _company_id(tok), "doc", f"doc:{tag}", {
        "doc_type": "invoice", "doc_number": f"INV-{tag}", "ref_id": f"INV-{tag}", "status": "draft",
        "contact_name": "Someone", "issue_date": "2026-10-01"})
    await session.flush()

    found = await _search(client, {"Authorization": f"Bearer {tok}"}, f"INV-{tag}")

    assert [r["id"] for r in found] == [f"doc:{tag}"]


async def test_a_document_is_found_by_its_contacts_name(client, session, docs_provider):
    tok = await register_admin(client)
    tag = uuid.uuid4().hex[:6].upper()
    _seed(session, _company_id(tok), "doc", f"doc:{tag}", {
        "doc_type": "invoice", "doc_number": f"INV-{tag}", "ref_id": f"INV-{tag}", "status": "draft",
        "contact_id": f"contact:{tag}", "contact_name": f"Zephyr {tag} Trading", "issue_date": "2026-10-01"})
    await session.flush()

    found = await _search(client, {"Authorization": f"Bearer {tok}"}, f"zephyr {tag}")

    assert [r["id"] for r in found] == [f"doc:{tag}"]


async def test_a_list_is_found_by_its_number(client, session, docs_provider):
    tok = await register_admin(client)
    tag = uuid.uuid4().hex[:4].upper()
    number = f"LST-2610-{tag}"
    _seed(session, _company_id(tok), "list", f"list:{tag}", {
        "list_type": "quotation", "ref_id": number, "status": "draft", "contact_name": "Someone"})
    await session.flush()

    found = await _search(client, {"Authorization": f"Bearer {tok}"}, number)

    assert [r["id"] for r in found] == [f"list:{tag}"]
    assert found[0]["kind"] == "list"


async def test_a_list_is_found_by_its_contacts_name(client, session, docs_provider):
    tok = await register_admin(client)
    tag = uuid.uuid4().hex[:6].upper()
    _seed(session, _company_id(tok), "list", f"list:{tag}", {
        "list_type": "quotation", "ref_id": f"LST-2610-{tag}", "status": "draft",
        "contact_id": f"contact:{tag}", "contact_name": f"Borealis {tag} Ltd"})
    await session.flush()

    found = await _search(client, {"Authorization": f"Bearer {tok}"}, f"borealis {tag}")

    assert [r["id"] for r in found] == [f"list:{tag}"]


async def test_another_companys_documents_are_not_found(client, session, docs_provider):
    from celerp.models.company import Company
    tok = await register_admin(client)
    tag = uuid.uuid4().hex[:6].upper()
    other = uuid.uuid4()
    session.add(Company(id=other, name="Other Co", slug=f"other-{other.hex[:8]}"))
    await session.flush()
    _seed(session, str(other), "doc", f"doc:{tag}", {
        "doc_type": "invoice", "doc_number": f"INV-{tag}", "ref_id": f"INV-{tag}", "contact_name": "Other"})
    _seed(session, str(other), "list", f"list:{tag}", {"list_type": "quotation", "ref_id": f"LST-{tag}"})
    await session.flush()

    assert await _search(client, {"Authorization": f"Bearer {tok}"}, tag) == []


async def test_a_role_without_view_documents_sees_neither(client, session, docs_provider):
    admin = {"Authorization": f"Bearer {await register_admin(client)}"}
    tag = uuid.uuid4().hex[:6].upper()
    company_id = _company_id(admin["Authorization"].split()[1])
    _seed(session, company_id, "doc", f"doc:{tag}", {"doc_type": "invoice", "doc_number": f"INV-{tag}", "ref_id": f"INV-{tag}"})
    _seed(session, company_id, "list", f"list:{tag}", {"list_type": "quotation", "ref_id": f"LST-{tag}"})
    await session.flush()
    await client.patch("/companies/me/role-permissions",
                       json={"perm_key": "view_documents", "role_key": "viewer", "granted": False}, headers=admin)
    viewer = {"Authorization": f"Bearer {await invite_user(client, session, admin, 'viewer@ex.test', 'viewer')}"}

    r = await client.get("/search", params={"q": tag}, headers=viewer)

    assert r.status_code == 200, r.text
    assert "celerp-docs" not in r.json()["results"]
    assert "celerp-docs" in (await client.get("/search", params={"q": tag}, headers=admin)).json()["results"]
