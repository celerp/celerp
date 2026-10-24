# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The document list, its sort and its CSV export name each document's contact as aging does:
the contact's current name, else the name the document holds, else nothing, never the contact's
id. Demo and imported documents name their contact by id only."""
from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

import pytest

from celerp.models.projections import Projection

pytestmark = pytest.mark.asyncio


async def _company(client) -> tuple[dict, uuid.UUID]:
    r = await client.post("/auth/register", json={"company_name": "List Names Co", "email": f"ln-{uuid.uuid4().hex[:8]}@docs.test",
                                                  "name": "Admin", "password": "validpass1"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    return headers, uuid.UUID((await client.get("/companies/me", headers=headers)).json()["id"])


async def _seed(session, company_id, entity_id: str, entity_type: str, state: dict) -> None:
    session.add(Projection(company_id=company_id, entity_id=entity_id, entity_type=entity_type, version=1,
                           updated_at=datetime.now(timezone.utc), state=state))
    await session.commit()


async def _invoice(session, company_id, doc_id: str, **party) -> None:
    """An issued, overdue invoice naming its party by ``party`` only."""
    await _seed(session, company_id, doc_id, "doc", {
        "doc_type": "invoice", "status": "final", "doc_number": doc_id, "currency": "USD", "line_items": [],
        "total": 40, "amount_outstanding": 40, "issue_date": "2026-01-01", "due_date": "2026-01-01", **party})


async def test_the_list_sort_and_export_name_the_contact_never_its_id(client, session):
    headers, company_id = await _company(client)
    known, renamed, gone = (f"contact:{uuid.uuid4()}" for _ in range(3))
    await _seed(session, company_id, known, "contact", {"name": "Harbour Supply Co", "contact_type": "customer"})
    await _seed(session, company_id, renamed, "contact", {"display_name": "Zephyr Trading", "contact_type": "customer"})
    await _invoice(session, company_id, "doc:by-id", contact_id=known)
    await _invoice(session, company_id, "doc:stale-name", contact_id=renamed, contact_name="Aaron Old Name")
    await _invoice(session, company_id, "doc:gone-named", contact_id=gone, contact_name="Walk-in Buyer")
    await _invoice(session, company_id, "doc:gone", contact_id=gone)
    await _invoice(session, company_id, "doc:no-party")
    shown = {"doc:by-id": "Harbour Supply Co", "doc:stale-name": "Zephyr Trading",
             "doc:gone-named": "Walk-in Buyer", "doc:gone": None, "doc:no-party": None}
    held = {"doc:by-id": known, "doc:stale-name": renamed, "doc:gone-named": gone, "doc:gone": gone, "doc:no-party": None}

    # Both list paths: every filter in SQL, and a filter that runs row by row (overdue).
    for extra in ({}, {"overdue_only": "true"}):
        r = await client.get("/docs", params={"doc_type": "invoice", "sort": "contact", "dir": "asc", **extra}, headers=headers)
        assert r.status_code == 200, r.text
        items = r.json()["items"]
        assert {d["id"]: d.get("contact_name") for d in items} == shown, extra
        # The id the document names is kept, so its link and filters still work.
        assert {d["id"]: d.get("contact_id") for d in items} == held, extra
        # The sort orders by the name shown: no name first, then by name.
        assert [d.get("contact_name") for d in items] == [None, None, "Harbour Supply Co", "Walk-in Buyer", "Zephyr Trading"], extra

    r = await client.get("/docs/export/csv", params={"doc_type": "invoice", "sort": "contact", "dir": "asc"}, headers=headers)
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {row["entity_id"]: row["contact_name"] or None for row in rows} == shown
    assert [row["contact_name"] for row in rows] == ["", "", "Harbour Supply Co", "Walk-in Buyer", "Zephyr Trading"]
    assert not any(str(v).startswith("contact:") for row in rows for k, v in row.items() if k == "contact_name")


async def test_a_search_finds_a_document_by_the_contact_name_it_shows(client, session):
    """Searching the list, or global search, for the name a document shows finds it, even when
    the document holds only its contact's id; the stored fields still match, another
    company's contact of the same id is never searched, and a wildcard character is
    searched for as itself."""
    from celerp_docs.search import global_search

    from celerp.models.company import Company

    headers, company_id = await _company(client)
    other_company = uuid.uuid4()
    session.add(Company(id=other_company, name="Other Co", slug=f"other-{other_company.hex[:8]}", settings={}))
    await session.commit()
    known, renamed, elsewhere = (f"contact:{uuid.uuid4()}" for _ in range(3))
    await _seed(session, company_id, known, "contact", {"name": "Harbour Supply Co", "contact_type": "customer"})
    await _seed(session, company_id, renamed, "contact", {"name": "Zephyr Trading", "contact_type": "customer"})
    await _seed(session, other_company, elsewhere, "contact", {"name": "Secret Customer Other", "contact_type": "customer"})
    await _invoice(session, company_id, "doc:by-id", contact_id=known)
    await _invoice(session, company_id, "doc:stale-name", contact_id=renamed, contact_name="Aaron Old Name")
    await _invoice(session, company_id, "doc:elsewhere", contact_id=elsewhere)
    await _invoice(session, company_id, "doc:50%_off\\x", contact_id=known)

    async def found(q: str) -> list[str]:
        r = await client.get("/docs", params={"doc_type": "invoice", "q": q}, headers=headers)
        assert r.status_code == 200, r.text
        listed = sorted(d["id"] for d in r.json()["items"])
        assert sorted(d["id"] for d in (await global_search(session, str(company_id), "owner", q, 20))["items"]) == listed
        return listed

    assert await found("Harbour") == ["doc:50%_off\\x", "doc:by-id"]
    assert await found("harbour, zephyr") == ["doc:50%_off\\x", "doc:by-id", "doc:stale-name"]
    assert await found("Aaron Old") == ["doc:stale-name"]
    assert await found("doc:by") == ["doc:by-id"]
    assert await found("Secret Customer") == []
    assert await found("O'Brien\" %") == []
    # A wildcard or the escape character in the search is the character itself.
    assert await found("%") == ["doc:50%_off\\x"]
    assert await found("_") == ["doc:50%_off\\x"]
    assert await found("\\") == ["doc:50%_off\\x"]
    assert await found("by_id") == []
    assert await found("50%off") == []


async def test_the_list_search_matches_a_wildcard_character_as_itself(client, session):
    """A % or _ in the list search is the character itself, never a wildcard."""
    headers, company_id = await _company(client)
    for list_id, ref in (("list:marked", "PL-50%_OFF"), ("list:plain", "PL-5000")):
        await _seed(session, company_id, list_id, "list", {"ref_id": ref, "status": "draft", "line_items": []})

    async def found(q: str) -> list[str]:
        r = await client.get("/lists", params={"q": q}, headers=headers)
        assert r.status_code == 200, r.text
        return sorted(d["id"] for d in r.json()["items"])

    assert await found("PL-50") == ["list:marked", "list:plain"]
    assert await found("%") == ["list:marked"]
    assert await found("_") == ["list:marked"]
    assert await found("pl-5_") == []
