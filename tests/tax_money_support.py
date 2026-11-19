# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared steps for the tax money tests: a company with books, tax codes saved through the
settings API, documents and payments through the documents API, the ledger read back by account
role, and the one rebuild comparator (the projections a replay of the ledger produces against
the ones stored), which the money proof script reuses."""
from __future__ import annotations

import re
import uuid

from sqlalchemy import select

from celerp.models.projections import Projection
from test_money_stock_and_contact_invariants import _account_net, _auth_company
from test_tax_acceptance_matrix import _books, _moved

__all__ = ["_account_net", "_auth_company", "_moved", "ISSUE", "PAID", "SUPPLIER", "company", "codes",
           "code", "line", "doc", "finalized", "get", "patch_doc", "pay", "post", "roles", "upload", "contact", "figures",
           "rebuild_divergence"]

ISSUE = "2026-10-01"
PAID = "2026-10-20"
SUPPLIER = "supplier:1"
_PATH = {"sales": "/companies/me/taxes", "purchase": "/companies/me/purchasing-taxes"}


async def company(session, currency: str = "USD") -> dict:
    return await _auth_company(session, currency)


def code(name: str, rate: float, side: str = "both", category: str = "standard", **fields) -> dict:
    return {"name": name, "tax_type": side, "category": category, "versions": [{"rate": rate}], **fields}


async def codes(client, auth, rows: list[dict], side: str = "sales") -> dict:
    """Save ``rows`` as the whole tax list of ``side``; the stored rows by name."""
    from celerp.localization.taxcodes import list_fingerprint
    h = auth["headers"]
    current = (await client.get(_PATH[side], headers=h)).json()
    r = await client.patch(_PATH[side], headers=h, json={"taxes": rows, "fingerprint": list_fingerprint(current)})
    assert r.status_code == 200, r.text
    return {row["name"]: row for row in (await client.get(_PATH[side], headers=h)).json()}


def line(price: float, *code_ids: str, quantity: float = 1, description: str = "Service") -> dict:
    return {"description": description, "quantity": quantity, "unit_price": price,
            "taxes": [{"code_id": c} for c in code_ids]}


async def doc(client, auth, doc_type: str, lines: list[dict], **fields) -> str:
    body = {"doc_type": doc_type, "issue_date": ISSUE, "line_items": lines, **fields}
    if doc_type in ("bill", "debit_note") and "contact_id" not in fields:
        body["contact_id"] = SUPPLIER
    r = await client.post("/docs", headers=auth["headers"], json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def get(client, auth, doc_id: str) -> dict:
    r = await client.get(f"/docs/{doc_id}", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return r.json()


async def patch_doc(client, auth, doc_id: str, field: str, value):
    from test_helpers import doc_version
    return await client.patch(f"/docs/{doc_id}", headers=auth["headers"], json={
        "expected_version": await doc_version(client, auth["headers"], doc_id),
        "fields_changed": {field: {"new": value}}})


async def post(client, auth, path: str, body: dict | None = None):
    return await client.post(path, headers=auth["headers"], json=body or {})


async def finalized(client, auth, doc_id: str) -> dict:
    r = await post(client, auth, f"/docs/{doc_id}/finalize")
    assert r.status_code == 200, r.text
    return await get(client, auth, doc_id)


async def pay(client, auth, doc_id: str, amount: float, withheld: list[dict] | None = None):
    body = {"amount": amount, "payment_date": PAID, "bank_account": "1111",
            "idempotency_key": f"pay-{uuid.uuid4().hex[:10]}"}
    if withheld is not None:
        body["withheld"] = withheld
    return await post(client, auth, f"/docs/{doc_id}/payment", body)


async def roles(session, auth) -> dict:
    """Debit minus credit of the company's posted journal entries, by account role."""
    return (await _books(session, auth))[0]


async def upload(client, auth, doc_id: str) -> str:
    r = await client.post(f"/docs/{doc_id}/files", headers=auth["headers"], files={
        "file": ("certificate.pdf", b"%PDF-1.0\n1 0 obj<</Type/Catalog>>endobj\n", "application/pdf")})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def contact(client, auth, name: str, contact_type: str, tax_ids: list[dict] | None = None) -> str:
    r = await client.post("/crm/contacts", headers=auth["headers"], json={"name": name, "contact_type": contact_type})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    if tax_ids is not None:
        r = await client.patch(f"/crm/contacts/{cid}", headers=auth["headers"],
                               json={"fields_changed": {"tax_ids": {"new": tax_ids}}})
        assert r.status_code == 200, r.text
    return cid


def figures(text: str) -> set[float]:
    """Every money figure in a message, commas dropped."""
    return {float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


async def rebuild_divergence(session, company_id) -> list[str]:
    """The entity ids whose stored projection differs from what a replay of the ledger builds.
    Replays under the company lock and commits, so the stored rows afterwards are the replay."""
    from celerp.projections.engine import ProjectionEngine
    from celerp.services.company_lock import lock_company

    async def _states() -> dict:
        session.expire_all()
        rows = (await session.execute(select(Projection).where(Projection.company_id == company_id))).scalars().all()
        return {row.entity_id: row.state for row in rows}

    stored = await _states()
    await lock_company(session, company_id)
    await ProjectionEngine.rebuild(session, company_id)
    await session.commit()
    replayed = await _states()
    return sorted(k for k in set(stored) | set(replayed) if stored.get(k) != replayed.get(k))
