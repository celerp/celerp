# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Contact search filters, orders and pages in SQL (GEN-009) and gives exactly what the former
Python path gave: the same rows in the same order, with the same deleted and type filtering.

``_python_search`` below is that former path, kept here as the reference; every case runs both
on the same seeded contacts (deleted in every falsy and truthy form, missing and blank names,
mixed case, Thai, accents, tags, LIKE wildcards in names, ties broken by id)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from celerp.models.projections import Projection
from celerp_contacts.references import CONTACT_TYPE_FILTER, contact_accepts
from celerp_contacts.search import count_contacts, global_search, search_contacts


def _match(contact: dict, q_lower: str) -> bool:
    return (
        q_lower in (contact.get("name") or "").lower()
        or q_lower in (contact.get("email") or "").lower()
        or q_lower in (contact.get("phone") or "").lower()
        or q_lower in (contact.get("company_name") or "").lower()
        or any(q_lower in t.lower() for t in (contact.get("tags") or []))
    )


def _python_search(contacts: list[dict], q: str, *, include_deleted=False, contact_type=None) -> list[dict]:
    """The search as it was before GEN-009 (load all, filter, sort in Python)."""
    results = list(contacts)
    if not include_deleted:
        results = [c for c in results if not c.get("deleted")]
    if q:
        q_lower = q.lower()
        results = [c for c in results if _match(c, q_lower)]
    if contact_type and contact_type in CONTACT_TYPE_FILTER:
        results = [c for c in results if contact_accepts(c, contact_type)]
    results.sort(key=lambda c: ((c.get("name") or "").lower(), c.get("id") or ""))
    return results


_STATES = [
    {"name": "Alice Smith", "email": "alice@example.com", "contact_type": "customer", "tags": ["VIP", "gold"]},
    {"name": "alice smith", "email": "alice2@example.com", "contact_type": "vendor"},
    {"name": "ALICE SMITH", "phone": "+66 81 234 5678", "contact_type": "both"},
    {"name": "Bob's Shop", "company_name": "Bob Holdings", "contact_type": None},
    {"name": "bob", "contact_type": ""},
    {"name": "บริษัท สยาม จำกัด", "company_name": "สยาม", "contact_type": "customer"},
    {"name": "ร้านทองเยาวราช", "tags": ["ทอง"], "contact_type": "vendor"},
    {"name": "Élise Dupont", "email": "elise@example.fr"},
    {"name": "élise dupont", "contact_type": "both"},
    {"name": "Zoë", "tags": []},
    {"name": "100% Cotton_Co", "contact_type": "customer"},
    {"name": "100 Cotton Co"},
    {"name": "Back\\slash Ltd"},
    {"name": None, "email": "noname@example.com"},
    {"email": "missing-name@example.com", "contact_type": "vendor"},
    {"name": "", "phone": "0800"},
    {"name": "Deleted True", "deleted": True},
    {"name": "Deleted False", "deleted": False},
    {"name": "Deleted Null", "deleted": None},
    {"name": "Deleted Zero", "deleted": 0},
    {"name": "Deleted One", "deleted": 1},
    {"name": "Deleted Empty String", "deleted": ""},
    {"name": "Deleted String", "deleted": "yes"},
    {"name": "Deleted Empty List", "deleted": []},
    {"name": "Deleted List", "deleted": [1]},
    {"name": "Deleted Empty Object", "deleted": {}},
    {"name": "Deleted Object", "deleted": {"at": "2026-10-29"}},
    {"name": "Tagged", "tags": ["Wholesale", "Bangkok"], "contact_type": "customer"},
    {"name": "Tags Not A List", "tags": None},
]
# Same name twice: the id decides the order.
_TWINS = [{"name": "Twin"}, {"name": "Twin"}, {"name": "twin"}]
_QUERIES = ["", "alice", "ALICE", "smith", "bob", "BOB'S", "สยาม", "ทอง", "élise", "ÉLISE", "elise", "zoë",
            "%", "_", "100%", "cotton_", "\\", "back\\", "example", "+66", "0800", "vip", "gold", "wholesale",
            "bangkok", "deleted", "twin", "nobody", "e"]


@pytest.fixture
async def seeded(session, auth, ids):
    company_id = ids["company_id"]
    now = datetime.now(timezone.utc)
    contacts = []
    for state in _STATES + _TWINS:
        eid = f"contact:{uuid.uuid4()}"
        session.add(Projection(entity_id=eid, company_id=company_id, entity_type="contact",
                               state=state, version=1, updated_at=now))
        contacts.append(state | {"id": eid})
    # A row of another entity type in the same company never shows.
    session.add(Projection(entity_id=f"item:{uuid.uuid4()}", company_id=company_id,
                           entity_type="item", state={"name": "Alice Item"}, version=1, updated_at=now))
    await session.flush()
    return company_id, contacts


@pytest.mark.asyncio
@pytest.mark.parametrize("include_deleted", [False, True])
@pytest.mark.parametrize("contact_type", [None, "customer", "vendor", "both", "unknown"])
async def test_sql_search_matches_the_python_search(session, seeded, include_deleted, contact_type):
    company_id, contacts = seeded
    for q in _QUERIES:
        expected = _python_search(contacts, q, include_deleted=include_deleted, contact_type=contact_type)
        got = await search_contacts(session, company_id, q, include_deleted=include_deleted, contact_type=contact_type)
        assert [c["id"] for c in got] == [c["id"] for c in expected], (q, contact_type, include_deleted)
        assert got == expected, q
        assert await count_contacts(session, company_id, q, include_deleted=include_deleted,
                                    contact_type=contact_type) == len(expected), q


@pytest.mark.asyncio
async def test_pages_are_the_python_slices(session, seeded):
    company_id, contacts = seeded
    expected = _python_search(contacts, "")
    for limit, offset in ((5, 0), (5, 5), (7, 3), (20, 0), (3, len(expected) - 2), (10, len(expected) + 5), (0, 0)):
        got = await search_contacts(session, company_id, "", limit=limit, offset=offset)
        assert [c["id"] for c in got] == [c["id"] for c in expected[offset:offset + limit]], (limit, offset)


@pytest.mark.asyncio
async def test_global_search_is_the_first_matches(session, seeded):
    company_id, contacts = seeded
    found = await global_search(session, company_id, "owner", "e", 4)
    assert [c["id"] for c in found["items"]] == [c["id"] for c in _python_search(contacts, "e")[:4]]


@pytest.mark.asyncio
async def test_the_list_route_pages_and_counts_in_sql(client, auth):
    tag = uuid.uuid4().hex[:6]
    for name in (f"Zed {tag}", f"amy {tag}", f"Bea {tag}"):
        r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": name, "contact_type": "customer"})
        assert r.status_code in (200, 201), r.text
    r = await client.get(f"/crm/contacts?q={tag}&limit=2&offset=0", headers=auth["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert [c["name"] for c in body["items"]] == [f"amy {tag}", f"Bea {tag}"]
    second = (await client.get(f"/crm/contacts?q={tag}&limit=2&offset=2", headers=auth["headers"])).json()
    assert [c["name"] for c in second["items"]] == [f"Zed {tag}"] and second["total"] == 3
