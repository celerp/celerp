# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company as an earlier version left it, for the upgrade tests: tax lists with names and
rates only, a purchase withholding row saved as a negative rate, a code no category fits, a
posting-role map without the withheld-tax role, and documents and items written in that
version's shape (a bill whose withholding is a negative line tax, posted as input tax less the
withholding). Then the upgrade step every company goes through."""
from __future__ import annotations

import uuid

from sqlalchemy import func, select

from celerp.models.ledger import LedgerEntry
from tax_money_support import ISSUE, SUPPLIER, company

G = 1000.0
_OLD_TAXES = [{"name": "VAT 7%", "rate": 7.0, "tax_type": "sales", "is_default": True, "description": ""}]
_OLD_PURCHASE = [
    {"name": "Input VAT 7%", "rate": 7.0, "tax_type": "purchase", "is_default": True, "description": ""},
    {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False, "description": ""},
    {"name": "Mystery 5%", "rate": 5.0, "tax_type": "purchase", "is_default": False, "description": ""},
]


def _old_tax(name: str, rate: float, order: int) -> dict:
    return {"code": name, "rate": rate, "amount": round(G * rate / 100, 2), "order": order,
            "is_compound": False, "label": name}


async def old_shape_company(session, *, restored_elsewhere: bool = False) -> dict:
    """A provisioned company rewritten to the earlier version's settings."""
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.account_roles import role_map
    from celerp.services.company_lock import locked_company

    auth = await company(session)
    row = await locked_company(session, auth["company_id"])
    settings = {k: v for k, v in (row.settings or {}).items() if k not in ("country", "localization")}
    settings.update({"taxes": _OLD_TAXES, "purchasing_taxes": _OLD_PURCHASE,
                     ROLES_KEY: {k: v for k, v in role_map(row.settings).items() if k != "tax_withheld_payable"}})
    if restored_elsewhere:
        settings["restored_backup"] = {"backup_id": "b-elsewhere"}
    row.settings = settings
    await session.commit()
    return auth


async def _emit_doc(session, auth, doc_id: str, data: dict) -> None:
    from celerp.events.engine import emit_event

    await emit_event(session, company_id=auth["company_id"], entity_id=doc_id, entity_type="doc",
                     event_type="doc.created", actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=uuid.uuid4().hex, metadata_={}, data=data)


async def _emit_je(session, auth, doc_id: str, ref: str, entries: list[dict]) -> None:
    from celerp.services.auto_je import _emit_auto_posted_je

    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=f"je:auto:{doc_id}:fin",
        record_id=doc_id, idem_create=uuid.uuid4().hex, idem_posted=uuid.uuid4().hex, memo=ref,
        metadata_={"source_doc": doc_id}, ts=f"{ISSUE}T00:00:00+00:00", entries=entries)


async def old_bill(session, auth, *, doc_type: str = "bill", status: str = "final",
                   original_doc_id: str | None = None, fund_id: str | None = None) -> str:
    """A supplier document in the earlier shape: input VAT 7% 70 and WHT 3% as rate -3 amount -30 on
    one line of 1000, tax 40, total 1040. A final one carries the entry that version posted
    (expense 1000 and input 40 against payable 1040; mirrored for a supplier note). ``fund_id``
    puts the line, and so every line of its entry, in that fund."""
    from celerp.accounting_roles import AccountRole as R
    from celerp.services.auto_je import _line

    from celerp.services.journal_accounts import hold_fund_ledger

    await hold_fund_ledger(session, auth["company_id"])  # before the document row: its entry may name a fund
    ref = f"OLD-{doc_type.upper()}-{uuid.uuid4().hex[:6]}"
    doc_id = f"doc:{ref}"
    data = {"doc_type": doc_type, "status": status, "ref_id": ref, "contact_id": SUPPLIER, "issue_date": ISSUE,
            "currency": "USD", "subtotal": G, "tax": 40.0, "total": 1040.0, "amount_paid": 0.0,
            "amount_outstanding": 1040.0 if status == "final" else 0.0,
            "line_items": [{"description": "Service", "quantity": 1, "unit_price": G, "line_total": G,
                            "taxes": [_old_tax("Input VAT 7%", 7.0, 0), _old_tax("WHT 3%", -3.0, 1)],
                            **({"fund_id": fund_id} if fund_id else {})}]}
    if original_doc_id:
        data["original_doc_id"] = original_doc_id
    await _emit_doc(session, auth, doc_id, data)
    if status == "final":
        debit = doc_type == "bill"
        await _emit_je(session, auth, doc_id, ref, [{**e, **({"fund_id": fund_id} if fund_id else {})} for e in [
            _line("6950", R.GENERAL_EXPENSE, **{"debit" if debit else "credit": G}),
            _line("1150", R.TAX_INPUT, **{"debit" if debit else "credit": 40.0}),
            _line("2110", R.PAYABLE, **{"credit" if debit else "debit": 1040.0})]])
    await session.commit()
    return doc_id


async def old_invoice(session, auth) -> str:
    """A final sales invoice in the earlier shape with VAT 7% only (no withholding), and its entry."""
    from celerp.accounting_roles import AccountRole as R
    from celerp.services.auto_je import _line

    ref = f"OLD-INV-{uuid.uuid4().hex[:6]}"
    doc_id = f"doc:{ref}"
    await _emit_doc(session, auth, doc_id, {
        "doc_type": "invoice", "status": "final", "ref_id": ref, "contact_name": "Buyer", "issue_date": ISSUE,
        "currency": "USD", "subtotal": G, "tax": 70.0, "total": 1070.0, "amount_paid": 0.0,
        "amount_outstanding": 1070.0,
        "line_items": [{"description": "Service", "quantity": 1, "unit_price": G, "line_total": G,
                        "taxes": [_old_tax("VAT 7%", 7.0, 0)]}]})
    await _emit_je(session, auth, doc_id, ref, [
        _line("1120", R.RECEIVABLE, debit=1070.0), _line("4100", R.SALES_REVENUE, credit=G),
        _line("2120", R.TAX_OUTPUT, credit=70.0)])
    await session.commit()
    return doc_id


async def old_item(client, auth, sku: str, tax_codes: list[str]) -> str:
    r = await client.post("/items", headers=auth["headers"],
                          json={"sku": sku, "name": sku, "quantity": 1, "sell_by": "piece", "tax_codes": tax_codes})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def upgrade(session, auth) -> dict:
    """The upgrade step the migration runs for each company; returns its notice counts."""
    from celerp.localization.upgrade import upgrade_company

    counts = await upgrade_company(session, auth["company_id"])
    await session.commit()
    session.expire_all()
    return counts


async def ledger_rows(session, auth) -> int:
    return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"]))).scalar_one()
