# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The suggestion ladder: each rung answers its own evidence and stops the ladder, a
contact appears only when a record names it, the model only picks from the posting
accounts, and the bucketed matcher returns what the old matcher returned."""

from __future__ import annotations

from decimal import Decimal

import pytest

from celerp_accounting import matcher
from celerp_accounting.statement_reader import counterparty_key
from celerp_accounting.suggest import NO_CONTACT_REASON, LadderContext, run_ladder

BANK = "bank-a"
OTHER_BANK = "bank-b"
ACCOUNTS = ["5100", "5200", "6100", "4000"]


def _line(lid, d, amount, desc, reference=None):
    key, account = counterparty_key(desc, reference, Decimal(amount))
    return {"id": lid, "line_date": d, "amount": Decimal(amount), "description": desc, "reference": reference,
            "key": key, "counterparty_account": account}


def _ctx(**kw):
    base = dict(bank_id=BANK, entries=[], open_docs=[], rules=[], pair_lines=[], slips=[], history={},
                accounts=ACCOUNTS, model=None)
    base.update(kw)
    return LadderContext(**base)


def _rule(**kw):
    r = {"id": "r1", "bank_account_id": None, "match_field": "description", "match_pattern": "NETFLIX",
         "match_type": "contains", "match_amount": None, "target_account_code": "6100", "contact_id": None,
         "target_bank_id": None, "origin": "user", "is_active": True}
    r.update(kw)
    return r


def _model_returning(account="6100", contact="contact-x"):
    calls = []

    def model(groups, accounts):
        calls.append(groups)
        return {g["key"]: {"account_code": account, "contact_id": contact, "reason": "Looks like software"}
                for g in groups}
    model.calls = calls
    return model


# rung -> (lines, context, line id asked, expected source, expected kind)
def _rungs():
    reversal = [_line("a", "2026-01-03", "-50.00", "ACME LTD PAYMENT"),
                _line("b", "2026-01-05", "50.00", "ACME LTD PAYMENT")]
    match = [_line("m", "2026-01-10", "-120.00", "OFFICE DEPOT 1199")]
    entry = {"je_id": "je:1", "date": "2026-01-09", "amount": Decimal("-120.00"), "memo": "Office Depot",
             "reference": "", "contact": None}
    doc_line = [_line("d", "2026-01-12", "1500.00", "ACME LTD", "INV-1")]
    amount_line = [_line("e", "2026-01-12", "1500.00", "ACME LTD")]
    doc = {"doc_id": "doc:inv1", "doc_type": "invoice", "contact_id": "contact-acme",
           "open_balance": Decimal("1500.00"), "ref": "INV-1", "identifier": "inv-1"}
    rule_line = [_line("r", "2026-01-15", "-15.99", "NETFLIX.COM")]
    transfer_line = [_line("t", "2026-01-20", "-1000.00", "TRANSFER TO OWN SAVINGS")]
    pair = {"id": "p", "bank_id": OTHER_BANK, "line_date": "2026-01-21", "amount": Decimal("1000.00"),
            "key": "TRANSFER FROM OWN"}
    slip_line = [_line("s", "2026-01-22", "-107.00", "POS STATIONERY SHOP")]
    slip = {"file_id": "f1", "date": "2026-01-22", "total": Decimal("107.00"), "tax": Decimal("7.00"),
            "tax_id": "0105551234567", "is_tax_invoice": True, "party_contact_id": "contact-shop"}
    hist_line = [_line("h", "2026-01-25", "-30.00", "GRAB TRIP")]
    history = {"GRAB TRIP": [{"account_code": "5200", "contact_id": "contact-grab"}] * 3}
    model_line = [_line("x", "2026-01-26", "-49.00", "SOMETHING NEW CO")]
    q_line = [_line("q", "2026-01-27", "-49.00", "SOMETHING NEW CO")]
    return [
        ("0-reversal", reversal, _ctx(), "a", "reversal", "no_entry"),
        ("1-match", match, _ctx(entries=[entry]), "m", "match", "match"),
        ("2-document", doc_line, _ctx(open_docs=[doc]), "d", "document", "pay_doc"),
        ("2-document-amount", amount_line, _ctx(open_docs=[doc]), "e", "document_amount", "pay_doc"),
        ("3-rule", rule_line, _ctx(rules=[_rule()]), "r", "rule", "post"),
        ("4-transfer", transfer_line, _ctx(pair_lines=[pair]), "t", "transfer", "transfer"),
        ("5-slip", slip_line, _ctx(slips=[slip]), "s", "slip", "post"),
        ("6-history", hist_line, _ctx(history=history), "h", "history", "post"),
        ("7-model", model_line, _ctx(model=_model_returning()), "x", "model", "post"),
        ("8-question", q_line, _ctx(), "q", "question", None),
    ]


@pytest.mark.parametrize("name,lines,ctx,lid,source,kind", _rungs(), ids=[r[0] for r in _rungs()])
def test_each_ladder_rung(name, lines, ctx, lid, source, kind):
    out = run_ladder(lines, ctx)
    s = out[lid]
    assert s["source"] == source and s["kind"] == kind
    if ctx.model is not None and source != "model":
        assert not ctx.model.calls, "a rung answered, the model must not be asked"


def test_rung_order_stops_at_first_answer():
    line = [_line("r", "2026-01-15", "-15.99", "NETFLIX.COM")]
    hist = {line[0]["key"]: [{"account_code": "5200", "contact_id": None}] * 5}
    model = _model_returning()
    out = run_ladder(line, _ctx(rules=[_rule()], history=hist, model=model))
    assert out["r"]["source"] == "rule" and not model.calls


@pytest.mark.parametrize("desc", ["WITHDRAWAL", "TRANSFER", "INV-88231"])
def test_no_rung_names_a_contact_without_evidence(desc):
    ref = desc if desc.startswith("INV") else None
    line = _line("s", "2026-02-02", "-200.00", desc, ref)
    assert line["key"].startswith("SPARSE:")
    model = _model_returning(contact="contact-made-up")
    out = run_ladder([line], _ctx(model=model))
    assert out["s"].get("contact_id") is None
    assert out["s"]["reason"].endswith(NO_CONTACT_REASON) or out["s"]["source"] == "question"
    # Sparse history answers an account only, and only for a matching amount.
    hist = {line["key"]: [{"account_code": "5200", "contact_id": "contact-h", "amount": Decimal("-200.00")}] * 2}
    out = run_ladder([line], _ctx(history=hist))
    assert out["s"]["source"] == "history" and out["s"].get("contact_id") is None
    other = {line["key"]: [{"account_code": "5200", "contact_id": "contact-h", "amount": Decimal("-75.00")}] * 2}
    assert run_ladder([line], _ctx(history=other))["s"]["source"] != "history"


def test_contact_kept_only_with_evidence():
    out = run_ladder([_line("d", "2026-01-12", "1500.00", "ACME LTD")], _ctx(open_docs=[
        {"doc_id": "doc:inv1", "doc_type": "invoice", "contact_id": "contact-acme",
         "open_balance": Decimal("1500.00"), "ref": "INV-1"}]))
    assert out["d"]["contact_id"] == "contact-acme"
    out = run_ladder([_line("x", "2026-01-26", "-49.00", "SOMETHING NEW CO")], _ctx(model=_model_returning()))
    assert out["x"].get("contact_id") is None and NO_CONTACT_REASON in out["x"]["reason"]


def test_model_output_outside_account_list_dropped():
    model = _model_returning(account="9999")
    out = run_ladder([_line("x", "2026-01-26", "-49.00", "SOMETHING NEW CO")], _ctx(model=model))
    assert out["x"]["source"] == "question" and out["x"].get("account_code") is None


def test_model_sees_residue_groups_only_in_batches_of_100():
    lines = [_line(f"l{i}", "2026-01-26", "-1.00", f"VENDOR{i:03d} CO") for i in range(150)]
    lines.append(_line("r", "2026-01-15", "-15.99", "NETFLIX.COM"))
    model = _model_returning()
    run_ladder(lines, _ctx(rules=[_rule()], model=model))
    assert [len(c) for c in model.calls] == [100, 50]
    assert all("NETFLIX" not in g["key"] for c in model.calls for g in c)


def test_reference_match_requires_amount():
    stmt = [{"id": "l1", "line_date": "2026-01-10", "amount": -100.0, "description": "INV-77 ACME", "reference": "INV-77"}]
    entries = [{"je_id": "je:1", "date": "2026-01-10", "amount": -95.0, "memo": "INV-77", "reference": "INV-77"}]
    assert matcher.auto_match(stmt, entries) == [("l1", "je:1", "low")]
    entries[0]["amount"] = -100.0
    assert matcher.auto_match(stmt, entries) == [("l1", "je:1", "high")]


def test_auto_match_buckets_equal_old_result(monkeypatch):
    stmt = [{"id": f"l{i}", "line_date": f"2026-01-{i + 1:02d}", "amount": float(-10 * (i + 1)),
             "description": f"Vendor {i}", "reference": None} for i in range(20)]
    entries = [{"je_id": f"je:{i}", "date": f"2026-01-{i + 1:02d}", "amount": float(-10 * (i + 1)),
                "memo": f"Vendor {i}", "reference": ""} for i in range(20)]
    seen = []
    real = matcher.amount_buckets
    monkeypatch.setattr(matcher, "amount_buckets", lambda rows: seen.append(len(rows)) or real(rows))
    got = matcher.auto_match(stmt, entries)
    assert seen, "auto_match reads entries through their amount buckets"
    assert sorted(got) == sorted((f"l{i}", f"je:{i}", "high") for i in range(20))


def test_combination_match_is_low():
    line = [_line("c", "2026-01-10", "300.00", "ACME LTD")]
    entries = [{"je_id": f"je:{i}", "date": "2026-01-08", "amount": Decimal("100.00"), "memo": "ACME",
                "reference": "", "contact": "contact-acme"} for i in range(3)]
    out = run_ladder(line, _ctx(entries=entries))
    assert out["c"]["kind"] == "match" and sorted(out["c"]["je_ids"]) == ["je:0", "je:1", "je:2"]
    assert out["c"]["confidence"] == "low"


@pytest.mark.parametrize("tax_id,total,vat", [
    ("0105551234567", "107.00", True), (None, "107.00", False), ("0105551234567", "110.00", False)],
    ids=["tax-id-equal-total", "no-tax-id", "total-differs"])
def test_vat_split_needs_tax_id_and_equal_total(tax_id, total, vat):
    line = [_line("s", "2026-01-22", "-107.00", "POS STATIONERY SHOP")]
    slip = {"file_id": "f1", "date": "2026-01-22", "total": Decimal(total), "tax": Decimal("7.00"),
            "tax_id": tax_id, "is_tax_invoice": True, "party_contact_id": None}
    out = run_ladder(line, _ctx(slips=[slip]))["s"]
    if total != "107.00":
        assert out["source"] != "slip"
        return
    assert out["source"] == "slip"
    assert bool(out.get("vat")) is vat
    if vat:
        assert out["vat"] == {"tax": "7.00", "net": "100.00"}


def test_a_line_with_several_candidates_names_the_first_document_and_the_nearest_transfer():
    """Two open invoices for the same money and no reference: the first open one is offered.
    Two transfer candidates on one other bank: the reason dates the nearest, not the list's last."""
    docs = [{"doc_id": f"doc:{n}", "doc_type": "invoice", "contact_id": None, "open_balance": Decimal("1500.00"),
             "ref": n.upper()} for n in ("inv1", "inv2")]
    out = run_ladder([_line("e", "2026-01-12", "1500.00", "ACME LTD")], _ctx(open_docs=docs))
    assert out["e"]["docs"][0]["doc_id"] == "doc:inv1"
    pairs = [{"id": i, "bank_id": OTHER_BANK, "line_date": d, "amount": Decimal("1000.00"), "key": "TRANSFER FROM OWN"}
             for i, d in (("near", "2026-01-21"), ("far", "2026-01-23"))]
    out = run_ladder([_line("t", "2026-01-20", "-1000.00", "TRANSFER TO OWN SAVINGS")], _ctx(pair_lines=pairs))
    assert "2026-01-21" in out["t"]["reason"] and "2026-01-23" not in out["t"]["reason"]
