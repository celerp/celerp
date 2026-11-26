# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tax money across the whole ledger: every tax category posted through every posting path
balances and rebuilds to the stored figures; the new posting roles are mapped on a chart Celerp
seeded and refused on a chart from elsewhere, never invented; and every reader of a document's
tax, total and owed figure gives the same answer for every document shape, with the owed figure
(total less what is withheld on the document) equal to what the ledger posts to payables or
receivables."""
from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest

from tax_money_support import (_moved, code, codes, company, doc, every_path, finalized, get, line, ok, pay, post,
                               rebuild_divergence, roles, unbalanced_entries)

pytestmark = pytest.mark.asyncio


def _wht(name: str, rate: float, side: str, point: str) -> dict:
    return code(name, rate, side, "withholding", withholding_point=point)


# ── every category on every posting path ────────────────────────────────────────

_CATEGORY_ROWS = {
    "standard": [],
    "reduced": [("Reduced", 3.0, "reduced")],
    "zero": [("Zero", 0.0, "zero")],
    "exempt": [("Exempt", 0.0, "exempt")],
    "out_of_scope": [("Outside", 0.0, "out_of_scope")],
    "reverse_charge": [("Reverse charge", 7.0, "reverse_charge")],
    "self_assessed_import": [("Import self-assessed", 7.0, "self_assessed_import")],
    "perception": [("Perception", 3.0, "perception")],
}


@pytest.mark.parametrize("category", [*_CATEGORY_ROWS, "withholding_document", "withholding_payment"])
async def test_category_postings_balance_matrix(client, session, category):
    """Each category rides beside a standard 7% code on both sides and goes through every
    posting path (finalize, notes, payments, voids, revert, note application, admin re-post);
    every posted entry balances, the account roles net to zero and a rebuild from the ledger
    gives back the stored projections. The admin re-post is the integrity doctor's missing
    entry fix, the one route that re-posts an invoice."""
    auth = await company(session)
    extra = {side: [code(f"{name} {side}", rate, side, cat) for name, rate, cat in _CATEGORY_ROWS.get(category, [])]
             for side in ("sales", "purchase")}
    point = {"withholding_document": "document", "withholding_payment": "payment"}.get(category)
    if point:
        extra = {side: [_wht(f"WHT {side}", 3.0, side, point)] for side in ("sales", "purchase")}
    sale = await codes(client, auth, [code("VAT", 7, "sales"), *extra["sales"]])
    buy = await codes(client, auth, [code("Input VAT", 7, "purchase"), *extra["purchase"]], "purchase")
    on_line = {side: [row["id"] for row in saved.values() if point != "payment" or row["category"] != "withholding"]
               for side, saved in (("sales", sale), ("purchase", buy))}
    at_payment = {side: next((row["id"] for row in saved.values() if point == "payment"
                              and row["category"] == "withholding"), None)
                  for side, saved in (("sales", sale), ("purchase", buy))}

    await every_path(client, auth, sale=on_line["sales"], buy=on_line["purchase"],
                     sale_wht=at_payment["sales"], buy_wht=at_payment["purchase"])
    assert await unbalanced_entries(session, auth) == []
    assert round(sum((await roles(session, auth)).values()), 6) == 0
    assert await rebuild_divergence(session, auth["company_id"]) == []


# ── posting roles ───────────────────────────────────────────────────────────────

async def _posting_roles(client, auth) -> tuple[dict, dict]:
    accounts = ok(await client.get("/accounting/posting-accounts", headers=auth["headers"]))
    chart = {a["code"]: a for a in ok(await client.get("/accounting/chart", headers=auth["headers"]))["items"]}
    return {a["role"]: a["code"] for a in accounts["roles"]}, chart


async def test_new_roles_mapped_or_refused_never_invented(client, session):
    """The withheld-tax payable role is seeded at 2130 and an upgraded company whose chart
    Celerp seeded gets it mapped there by the reconcile. The first sales withholding code adds
    the withheld-tax credit account 1180 and the first cash-basis code adds the pending input
    and output accounts 1155 and 2125. On a chart restored from elsewhere saving such a code
    is refused, naming the role and where to choose its account, and no account is made."""
    from celerp.accounting_roles import ROLES_KEY
    from celerp.services.account_roles import reconcile_company, role_map
    from celerp.services.company_lock import locked_company
    from test_l10n_country_update import _apply_country
    from ui.i18n import refusal_text

    fresh = await company(session)
    mapped, _ = await _posting_roles(client, fresh)
    assert mapped.get("tax_withheld_payable") == "2130"

    upgraded = await company(session)
    row = await locked_company(session, upgraded["company_id"])
    kept = {k: v for k, v in role_map(row.settings).items() if k != "tax_withheld_payable"}
    row.settings = {**row.settings, ROLES_KEY: kept}
    await session.commit()
    assert (await _posting_roles(client, upgraded))[0].get("tax_withheld_payable") is None
    await reconcile_company(session, upgraded["company_id"])
    await session.commit()
    assert (await _posting_roles(client, upgraded))[0].get("tax_withheld_payable") == "2130"

    await codes(client, fresh, [code("VAT", 7, "sales"), _wht("IRPF", 15, "sales", "document")])
    mapped, chart = await _posting_roles(client, fresh)
    assert mapped.get("tax_withheld_credit") == "1180" and "1180" in chart
    await _apply_country(client, fresh, "zzES")
    listed = ok(await client.get("/companies/me/taxes", headers=fresh["headers"]))
    await codes(client, fresh, [*listed, code("IVA caja", 21, "sales", cash_basis=True)])
    mapped, chart = await _posting_roles(client, fresh)
    assert (mapped.get("tax_input_pending"), mapped.get("tax_output_pending")) == ("1155", "2125")
    assert {"1155", "2125"} <= set(chart)

    restored = await company(session)
    row = await locked_company(session, restored["company_id"])
    row.settings = {**(row.settings or {}), "restored_backup": {"backup_id": "b-elsewhere"}}
    await session.commit()
    from celerp.localization.taxcodes import list_fingerprint
    current = ok(await client.get("/companies/me/taxes", headers=restored["headers"]))
    r = await client.patch("/companies/me/taxes", headers=restored["headers"], json={
        "taxes": [*current, _wht("IRPF", 15, "sales", "document")], "fingerprint": list_fingerprint(current)})
    assert r.status_code == 422, r.text
    text = refusal_text(r.json()["detail"])
    assert "Posting accounts" in text and "withheld" in text.lower()
    mapped, chart = await _posting_roles(client, restored)
    assert mapped.get("tax_withheld_credit") is None and "1180" not in chart


# ── readers by shape ────────────────────────────────────────────────────────────

_G = 1000.0


async def _legacy_bill(session, auth, vat: str, wht: str) -> str:
    """An issued bill from before the upgrade (L1) as the upgrade leaves it: its withholding
    line made positive, tax 70, total 1070, withheld 30 and the flag that its entry is still the
    old shape, with that entry (expense 1000 and input 40 against payable 1040)."""
    from celerp.events.engine import emit_event
    from celerp.services.auto_je import _emit_auto_posted_je, _line
    from celerp.accounting_roles import AccountRole

    ref = f"BILL-L1-{uuid.uuid4().hex[:6]}"
    taxes = [{"code_id": vat, "code": "Input VAT", "rate": 7.0, "amount": 70.0, "category": "standard"},
             {"code_id": wht, "code": "WHT 3", "rate": 3.0, "amount": 30.0, "category": "withholding",
              "withholding_point": "document"}]
    await emit_event(session, company_id=auth["company_id"], entity_id=f"doc:{ref}", entity_type="doc",
                     event_type="doc.created", actor_id=auth["user_id"], location_id=None, source="api",
                     idempotency_key=uuid.uuid4().hex, metadata_={}, data={
                         "doc_type": "bill", "status": "final", "ref_id": ref, "contact_id": "supplier:1",
                         "issue_date": "2026-10-01", "currency": "USD", "subtotal": _G, "tax": 70.0, "total": 1070.0,
                         "withheld": 30.0, "legacy_wht_entry": True, "amount_paid": 0.0, "amount_outstanding": 1040.0,
                         "line_items": [{"description": "Service", "quantity": 1, "unit_price": _G,
                                         "line_total": _G, "taxes": taxes}]})
    await _emit_auto_posted_je(
        session, company_id=auth["company_id"], user_id=auth["user_id"], je_id=f"je:auto:doc:{ref}:fin",
        record_id=f"doc:{ref}", idem_create=uuid.uuid4().hex, idem_posted=uuid.uuid4().hex, memo=ref,
        metadata_={"source_doc": f"doc:{ref}"}, ts="2026-10-01T00:00:00+00:00", entries=[
            _line("6950", AccountRole.GENERAL_EXPENSE, debit=_G), _line("1150", AccountRole.TAX_INPUT, debit=40.0),
            _line("2110", AccountRole.PAYABLE, credit=1040.0)])
    await session.commit()
    return f"doc:{ref}"


# shape: (document kind, lines from the saved codes, document fields, tax, total, withheld, owed)
_SHAPES = {
    "L1": ("bill", None, {}, 70.0, 1070.0, 30.0, 1040.0),
    "Nb": ("bill", lambda c: [line(_G, c["vat"], c["wht_doc"])], {}, 70.0, 1070.0, 30.0, 1040.0),
    "Ni": ("invoice", lambda c: [line(_G, c["vat"], c["wht_doc"])], {}, 70.0, 1070.0, 30.0, 1040.0),
    "Np": ("bill", lambda c: [line(_G, c["vat"])], {}, 70.0, 1070.0, 0.0, 1070.0),
    "Ns": ("invoice", lambda c: [line(_G, c["vat"])], {}, 70.0, 1070.0, 0.0, 1070.0),
    "Nq": ("bill", lambda c: [line(_G, c["vat"], c["perception"])], {}, 100.0, 1100.0, 0.0, 1100.0),
    "Nr-bill": ("bill", lambda c: [line(600.0, c["vat"]), line(400.0, c["rc"])], {}, 42.0, 1042.0, 0.0, 1042.0),
    "Nr-sale": ("invoice", lambda c: [line(600.0, c["vat"]), line(400.0, c["rc"])], {}, 42.0, 1042.0, 0.0, 1042.0),
    "Ninc-bill": ("bill", lambda c: [line(1070.0, c["vat"])], {"prices_include_tax": True}, 70.0, 1070.0, 0.0, 1070.0),
    "Ninc-sale": ("invoice", lambda c: [line(1070.0, c["vat"])], {"prices_include_tax": True},
                  70.0, 1070.0, 0.0, 1070.0),
    "Nch": ("bill", lambda c: [line(_G, c["vat"])], {"shipping": 100.0}, 77.0, 1177.0, 0.0, 1177.0),
    "Nc": ("bill", lambda c: [line(_G, c["cash"])], {}, 70.0, 1070.0, 0.0, 1070.0),
}


async def _shape_codes(client, auth, side: str, cash: bool) -> dict:
    tag = "sales" if side == "invoice" else "purchase"
    if cash:
        from test_l10n_country_update import _apply_country
        await _apply_country(client, auth, "zzES")
    path = "/companies/me/taxes" if tag == "sales" else "/companies/me/purchasing-taxes"
    kept = ok(await client.get(path, headers=auth["headers"])) if cash else []
    rows = [*kept, code("VAT", 7, tag), _wht("WHT doc", 3, tag, "document"), _wht("WHT pay", 2, tag, "payment"),
            code("Perception", 3, tag, "perception"), code("Reverse charge", 7, tag, "reverse_charge")]
    if cash:
        rows.append(code("VAT cash", 7, tag, cash_basis=True))
    saved = await codes(client, auth, rows, tag)
    return {"vat": saved["VAT"]["id"], "wht_doc": saved["WHT doc"]["id"], "wht_pay": saved["WHT pay"]["id"],
            "perception": saved["Perception"]["id"], "rc": saved["Reverse charge"]["id"],
            "cash": saved["VAT cash"]["id"] if cash else None}


async def _shaped(client, session, auth, shape: str, c: dict) -> dict:
    kind, lines, fields = _SHAPES[shape][:3]
    if shape == "L1":
        return await get(client, auth, await _legacy_bill(session, auth, c["vat"], c["wht_doc"]))
    return await finalized(client, auth, await doc(client, auth, kind, lines(c), **fields))


@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_tax_total_readers_by_shape(client, session, shape):
    """For each document shape every reader gives the shape's figures: tax, total, withheld and
    the owed figure, which equals what the ledger posted to payables or receivables. A part
    payment leaves owed less paid outstanding. A payment cannot withhold a code the document
    already withholds, nor withhold more than is outstanding (one cent over is refused, the
    exact amount settles it). A supplier note applies at most what the bill still owes (the
    old-shape bill refuses until its withholding is corrected), and on a sales invoice paid
    with withholding a credit note never takes the room below zero."""
    kind, _lines, _fields, tax, total, withheld, owed = _SHAPES[shape]
    auth = await company(session)
    c = await _shape_codes(client, auth, kind, shape == "Nc")
    account = "payable" if kind == "bill" else "receivable"
    sign = -1 if kind == "bill" else 1

    before = await roles(session, auth)
    state = await _shaped(client, session, auth, shape, c)
    assert (state["tax"], state["total"], state.get("withheld") or 0.0) == (tax, total, withheld)
    assert state["amount_outstanding"] == owed
    if shape != "L1":
        assert _moved(before, await roles(session, auth))[account] == sign * owed
    if shape.startswith("Ninc"):
        assert state["subtotal"] == _G
    if shape == "Nc":
        assert _moved(before, await roles(session, auth)).get("tax_input_pending") == 70.0

    ok(await pay(client, auth, state["id"], 100.0))
    outstanding = (await get(client, auth, state["id"]))["amount_outstanding"]
    assert outstanding == round(owed - 100.0, 2)

    if withheld:
        r = await pay(client, auth, state["id"], 10.0, [{"code_id": c["wht_doc"], "amount": 1.0}])
        assert r.status_code == 422, r.text
    over = await pay(client, auth, state["id"], 1.0, [{"code_id": c["wht_pay"], "amount": round(outstanding - 0.99, 2)}])
    assert over.status_code == 422, over.text
    ok(await pay(client, auth, state["id"], 1.0, [{"code_id": c["wht_pay"], "amount": round(outstanding - 1.0, 2)}]))
    paid = await get(client, auth, state["id"])
    assert (paid["status"], paid["amount_outstanding"]) == ("paid", 0.0)

    fresh = await _shaped(client, session, auth, shape, c)
    if kind == "bill":
        ok(await pay(client, auth, fresh["id"], 100.0))
        left = (await get(client, auth, fresh["id"]))["amount_outstanding"]
        lines = [line(2000.0, c["vat"])] if shape == "L1" else _SHAPES[shape][1](c)
        note = await doc(client, auth, "debit_note", [{**row, "unit_price": row["unit_price"] * 2} for row in lines],
                         original_doc_id=fresh["id"], **_SHAPES[shape][2])
        r = await post(client, auth, f"/docs/{note}/finalize")
        if shape == "L1":
            assert r.status_code == 409, r.text
            return
        assert r.status_code == 200, r.text
        note_state = await get(client, auth, note)
        note_owed = note_state["total"] - (note_state.get("withheld") or 0.0)
        assert (await get(client, auth, fresh["id"]))["amount_outstanding"] == 0.0
        assert note_state["amount_outstanding"] == round(note_owed - min(note_owed, left), 2)
        return

    ok(await pay(client, auth, fresh["id"], 500.0, [{"code_id": c["wht_pay"], "amount": 21.4}]))
    lines = _SHAPES[shape][1](c)
    # Issued on its own (a note naming its invoice applies itself on finalize), then applied whole.
    note = await finalized(client, auth, await doc(client, auth, "credit_note", lines, **_SHAPES[shape][2]))
    r = await post(client, auth, f"/docs/{note['id']}/apply-to-invoice", {
        "target_doc_id": fresh["id"], "amount": note["total"], "idempotency_key": uuid.uuid4().hex})
    assert r.status_code == 409, r.text
    assert (await get(client, auth, fresh["id"]))["amount_outstanding"] >= 0.0


# ── the reader list is closed ───────────────────────────────────────────────────

_SEARCHES = {
    "identity": r'get\("tax"|bill_line_charges\(|_doc_net_posted|total.{0,20}- .{0,30}tax',
    "amount_due": r'def _payment_balances|def outstanding_balance|setdefault\("amount_outstanding"'
                  r'|amount_outstanding"?\]? *[=:] *[^,]{0,40}total|amount_outstanding=data\["total"\]',
    "owed": r'"total"\).{0,60}(amount_paid|amount_outstanding|credited|_payable_balance)'
            r'|min\(to_decimal\(\w+\.get\("total"',
}
_ACC, _AI, _DOCS = ("default_modules/celerp-accounting/celerp_accounting/", "default_modules/celerp-ai/celerp_ai/",
                    "default_modules/celerp-docs/celerp_docs/")

# The readers of the tax readers table, by file and the text the search matched. A reader
# whose matched text changes is a changed reader: it is checked against the table again and
# its entry here updated with it.
READERS = {
    "identity": {
        ("celerp/services/auto_je.py", 'bill_line_charges('), ("celerp/services/auto_je.py", 'get("tax"'),
        ("celerp/services/auto_je.py", 'total", 0), currency) - round_money(doc.get("tax'),
        ("celerp/services/auto_je.py", 'total) - to_decimal(tax'), ("celerp/services/doc_balance.py", 'get("tax"'),
        ("celerp/services/landed_cost.py", 'bill_line_charges('), ("celerp/services/peer_lines.py", 'bill_line_charges('),
        ("celerp/services/posting_readiness.py", 'get("tax"'), (_ACC + "routes.py", '_doc_net_posted'),
        (_ACC + "routes.py", 'get("tax"'), (_AI + "routes.py", 'get("tax"'), (_AI + "routes.py", 'total = round(total - tax'),
        (_DOCS + "doc_money.py", 'get("tax"'), (_DOCS + "doc_projections.py", 'get("tax"'),
        (_DOCS + "migration_sink.py", 'total - tax'), (_DOCS + "routes.py", 'bill_line_charges('),
        (_DOCS + "routes.py", 'get("tax"'), (_DOCS + "routes_share.py", 'get("tax"'),
        ("ui/routes/documents.py", 'get("tax"'), ("ui/routes/documents.py", 'total - discount + tax'),
        # A scanned receipt's tax, read to suggest a bank line's posting: no document's figure.
        (_ACC + "suggest.py", 'get("tax"'),
        # The upgrade's one rewrite of a pre-upgrade document: its withheld amount moves back
        # into the tax figure as a withholding (P2.18), read once from the stored state.
        ("celerp/localization/upgrade.py", 'get("tax"'),
    },
    "amount_due": {
        ("celerp/services/doc_balance.py", 'def outstanding_balance'),
        (_DOCS + "doc_projections.py", 'def _payment_balances'),
        (_DOCS + "doc_projections.py", 'setdefault("amount_outstanding"'),
        (_DOCS + "doc_service.py", 'amount_outstanding": 0.0 if status == "closed" else total'),
        (_DOCS + "doc_service.py", 'amount_outstanding": total'), (_DOCS + "routes.py", 'amount_outstanding": total'),
        (_DOCS + "routes.py", 'amount_outstanding"] = float(data.get("total'),
        ("default_modules/celerp-fixed-assets/celerp_fixed_assets/demo.py", 'amount_outstanding": money["total'),
        ("default_modules/celerp-fixed-assets/celerp_fixed_assets/sales.py", 'amount_outstanding=data["total'),
        ("default_modules/celerp-subscriptions/celerp_subscriptions/billing.py", 'amount_outstanding": money["total'),
        ("default_modules/celerp-subscriptions/celerp_subscriptions/demo.py", 'amount_outstanding": money["total'),
    },
    "owed": {
        (_DOCS + "legacy_credit_notes.py", '"total") - money(inv, "amount_paid") - money(inv, "credited'),
        (_DOCS + "routes.py", '"total") - money(inv, "amount_paid'),
        (_DOCS + "routes.py", '"total") - money(note, "amount_paid'),
        (_DOCS + "routes.py", 'min(to_decimal(_initial_doc_state.get("total"'),
    },
}
_ROOT = Path(__file__).resolve().parent.parent


def _hits(pattern: str) -> set[tuple[str, str]]:
    out = subprocess.run(["git", "grep", "-n", "-P", pattern, "--", "celerp/*.py", "default_modules/*.py",
                          "ui/*.py", ":(exclude)*/tests/*"], cwd=_ROOT, capture_output=True, text=True)
    assert out.returncode in (0, 1), out.stderr
    found = set()
    for row in out.stdout.splitlines():
        path, _n, text = row.split(":", 2)
        found |= {(path, m.group(0)) for m in re.finditer(pattern, text)}
    return found


@pytest.mark.parametrize("search", list(_SEARCHES))
async def test_tax_total_readers_census_closed(search):
    """Every hit of the three reader searches is a reader the tax readers table lists, and every
    file holding an owed reader reads the owed figure through ``amount_due``."""
    from celerp.services import doc_balance
    assert callable(getattr(doc_balance, "amount_due", None))
    assert _hits(_SEARCHES[search]) - READERS[search] == set()
    if search != "identity":
        lacking = {path for path, _ in READERS[search] if "amount_due(" not in (_ROOT / path).read_text()}
        assert lacking == set()
