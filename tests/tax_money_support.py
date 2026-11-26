# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared steps for the tax money tests: a company with books, tax codes saved through the
settings API, documents and payments through the documents API, the ledger read back by account
role, and the one rebuild comparator (the projections a replay of the ledger produces against
the ones stored)."""
from __future__ import annotations

import re
import uuid

from sqlalchemy import select

from celerp.models.projections import Projection
from test_money_stock_and_contact_invariants import _account_net, _auth_company
from test_helpers import TAX_LIST_PATHS, save_taxes
from test_tax_acceptance_matrix import _books, _moved

__all__ = ["_account_net", "_auth_company", "_moved", "ISSUE", "PAID", "SUPPLIER", "company", "codes",
           "code", "line", "doc", "finalized", "get", "patch_doc", "pay", "post", "ok", "roles", "upload", "contact",
           "figures", "every_path", "unbalanced_entries", "rebuild_divergence"]

ISSUE = "2026-10-01"
PAID = "2026-10-20"
SUPPLIER = "supplier:1"


async def company(session, currency: str = "USD") -> dict:
    return await _auth_company(session, currency)


def code(name: str, rate: float, side: str = "both", category: str = "standard", **fields) -> dict:
    return {"name": name, "tax_type": side, "category": category, "versions": [{"rate": rate}], **fields}


async def codes(client, auth, rows: list[dict], side: str = "sales") -> dict:
    """Save ``rows`` as the whole tax list of ``side``; the stored rows by name."""
    h = auth["headers"]
    r = await save_taxes(client, h, rows, side)
    assert r.status_code == 200, r.text
    return {row["name"]: row for row in (await client.get(TAX_LIST_PATHS[side], headers=h)).json()}


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


async def pay(client, auth, doc_id: str, amount: float, withheld: list[dict] | None = None, date: str = PAID):
    body = {"amount": amount, "payment_date": date, "bank_account": "1111",
            "idempotency_key": f"pay-{uuid.uuid4().hex[:10]}"}
    if withheld is not None:
        body["withheld"] = withheld
    return await post(client, auth, f"/docs/{doc_id}/payment", body)


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def _key() -> str:
    return uuid.uuid4().hex[:12]


async def every_path(client, auth, *, sale: list[str], buy: list[str], sale_wht: str | None = None,
                     buy_wht: str | None = None) -> dict:
    """Post the documents through every posting path with the given codes on their lines:
    finalize a bill (PP1) and a supplier note that applies to it (PP2, PP11), finalize an
    invoice (PP3) and a credit note applied to it (PP4, PP11), pay both (PP5, withholding the
    payment-point code when one is given), void a payment (PP6), void and unvoid a document
    (PP7, PP8), revert and finalize again (PP9) and run the admin re-post of missing entries
    (PP10). The ids of the documents by role."""
    def withheld(code_id: str | None, amount: float) -> list[dict] | None:
        return [{"code_id": code_id, "amount": amount}] if code_id else None

    ids = {}
    for kind, note, codes_, wht in (("bill", "debit_note", buy, buy_wht), ("invoice", "credit_note", sale, sale_wht)):
        main = await finalized(client, auth, await doc(client, auth, kind, [line(1000.0, *codes_)]))
        ids[kind] = main["id"]
        # A supplier note issued against its bill is applied to it on finalize (PP2); the credit
        # note is issued on its own and applied through the route (PP11).
        against = {"original_doc_id": main["id"]} if kind == "bill" else {}
        note_doc = await finalized(client, auth, await doc(client, auth, note, [line(100.0, *codes_)], **against))
        ids[note] = note_doc["id"]
        if kind == "invoice":
            ok(await post(client, auth, f"/docs/{note_doc['id']}/apply-to-invoice", {
                "target_doc_id": main["id"], "amount": note_doc["total"] - (note_doc.get("withheld") or 0),
                "idempotency_key": _key()}))
        ok(await pay(client, auth, main["id"], 200.0, withheld(wht, 10.0)))
        ok(await post(client, auth, f"/docs/{main['id']}/void-payment", {"payment_index": 0, "idempotency_key": _key()}))
        ok(await pay(client, auth, main["id"], 200.0, withheld(wht, 10.0)))

        other = await finalized(client, auth, await doc(client, auth, kind, [line(500.0, *codes_)]))
        ids[f"{kind}_voided"] = other["id"]
        ok(await post(client, auth, f"/docs/{other['id']}/void", {"reason": "Entered twice", "idempotency_key": _key()}))
        ok(await post(client, auth, f"/docs/{other['id']}/unvoid", {"idempotency_key": _key()}))
        ok(await post(client, auth, f"/docs/{other['id']}/revert-to-draft", {"idempotency_key": _key()}))
        await finalized(client, auth, other["id"])
    ok(await post(client, auth, "/admin/doctor?checks=missing_jes&fix=true"))
    return ids


async def unbalanced_entries(session, auth) -> list[str]:
    """The posted journal entries whose debits and credits differ."""
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    return sorted(row.entity_id for row in rows if row.state.get("status") == "posted" and round(sum(
        float(e.get("debit") or 0) - float(e.get("credit") or 0) for e in row.state.get("entries") or []), 6))


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
