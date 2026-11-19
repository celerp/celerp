# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The statement reader: OFX, tie-out, sign convention, flagged rows, period, currency,
line identity and the counterparty key a group is built on. Pure functions, no database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from celerp_accounting.csv_parser import parse_bank_csv, parse_ofx
from celerp_accounting.statement_reader import counterparty_key, review_statement
from ui.i18n import t



def _line(d, amount, desc="Line", balance=None, reference=None, **kw):
    return {"line_date": d, "description": desc, "amount": amount, "raw_balance": balance, "reference": reference, **kw}


def _held(review: dict, indices=None, statement="2025-12-31") -> list[dict]:
    """The lines of ``review`` at ``indices`` (every line by default) as the bank holds them."""
    picked = range(len(review["lines"])) if indices is None else indices
    return [{**review["lines"][i], "statement": statement} for i in picked]


def _review(header, lines, **kw):
    kw.setdefault("bank_currency", "USD")
    return review_statement({"currency": "USD", **header}, lines, **kw)


_HEADER = {"period_start": "2026-01-01", "period_end": "2026-01-31", "opening_balance": "100.00"}


def test_tie_out_names_first_failing_line():
    lines = [_line("2026-01-02", "-10.00", balance="90.00"), _line("2026-01-03", "-5.00", balance="80.00"),
             _line("2026-01-04", "-5.00", balance="75.00")]
    out = _review({**_HEADER, "closing_balance": "80.00"}, lines)
    assert out["tie"]["ok"] is False
    assert out["tie"]["first_break"] == 1
    assert out["blocking"]


def test_card_statement_sign_flipped_and_stated():
    lines = [_line("2026-01-02", "25.00", "COFFEE"), _line("2026-01-05", "75.00", "HOTEL")]
    out = _review({**_HEADER, "opening_balance": "0.00", "closing_balance": "-100.00"}, lines)
    assert out["tie"]["ok"] is True and out["flipped"] is True
    assert [l["amount"] for l in out["lines"]] == ["-25.00", "-75.00"]
    assert out["notices"] and "reversed" in " ".join(out["notices"]).lower()


def test_row_without_amount_blocks_import():
    parsed = parse_bank_csv(b"Date,Description,Amount\n2026-01-02,Fee,\n2026-01-03,Wire,50\n")
    assert parsed["lines"][0]["amount"] is None
    assert parsed["lines"][0]["flags"] == ["no_amount"]
    out = _review({**_HEADER, "closing_balance": "150.00"}, parsed["lines"])
    assert any(b["code"] == "no_amount" and b["line"] == 0 for b in out["blocking"])


def test_line_outside_period_blocks_import():
    out = _review({**_HEADER, "closing_balance": "90.00"}, [_line("2026-02-02", "-10.00", "LATE")])
    assert any(b["code"] == "outside_period" and b["line"] == 0 for b in out["blocking"])


def test_statement_currency_must_match_bank():
    out = _review({**_HEADER, "currency": "THB", "closing_balance": "90.00"}, [_line("2026-01-02", "-10.00")])
    block = next(b for b in out["blocking"] if b["code"] == "currency")
    assert "THB" in block["text"] and "USD" in block["text"]


def test_fingerprints_number_identical_lines_in_a_file():
    lines = [_line("2026-01-02", "-3.00", "ATM FEE"), _line("2026-01-02", "-3.00", "ATM FEE")]
    out = _review({**_HEADER, "closing_balance": "94.00"}, lines)
    a, b = (l["fingerprint"] for l in out["lines"])
    assert a != b and len(a) == 16


def test_a_line_held_with_its_running_balance_is_a_confirmed_duplicate():
    lines = [_line("2026-01-02", "-10.00", "FEE", "90.00")]
    first = _review({**_HEADER, "closing_balance": "90.00"}, lines)
    line = _review({**_HEADER, "closing_balance": "90.00"}, lines, known=_held(first, statement="2026-01-31"))["lines"][0]
    assert line["duplicate"] == t("acct.recon_duplicate_of", date="2026-01-31")
    assert line["ticked"] is False and line["repeats"] is None


def test_a_second_identical_transaction_without_balance_or_fitid_is_left_for_review_and_importable():
    lines = [_line("2026-01-02", "-10.00", "FEE")]
    first = _review({**_HEADER, "closing_balance": "90.00"}, lines)
    line = _review({**_HEADER, "closing_balance": "90.00"}, lines, known=_held(first, statement="2026-01-31"))["lines"][0]
    assert line["duplicate"] is False and line["probable"] is True and line["repeats"] == "2026-01-31"
    assert line["ticked"] is False and line["fingerprint"] != first["lines"][0]["fingerprint"]


def test_a_statement_read_again_with_running_balances_is_a_duplicate_line_for_line():
    header = {**_HEADER, "closing_balance": "84.00"}
    lines = [_line("2026-01-02", "-3.00", "ATM FEE", "97.00"), _line("2026-01-02", "-3.00", "ATM FEE", "94.00"),
             _line("2026-01-03", "-10.00", "SHOP", "84.00")]
    first = _review(header, lines)
    again = _review(header, lines, known=_held(first))
    assert [bool(l["duplicate"]) for l in again["lines"]] == [True, True, True]
    assert [l["fingerprint"] for l in again["lines"]] == [l["fingerprint"] for l in first["lines"]]


def test_a_fitid_read_again_is_a_duplicate_and_another_fitid_is_a_new_line():
    ofx = [_line("2026-01-02", "-10.00", "FEE", reference="F1", source="ofx"),
           _line("2026-01-02", "-10.00", "FEE", reference="F2", source="ofx")]
    first = _review({**_HEADER, "closing_balance": "80.00"}, ofx[:1] + [_line("2026-01-05", "-10.00", "X")])
    again = _review({**_HEADER, "closing_balance": "80.00"}, ofx, known=_held(first, [0]))
    assert first["lines"][0]["fitid"] == "F1" and first["lines"][0]["reference"] == "F1"
    assert bool(again["lines"][0]["duplicate"]) is True
    assert again["lines"][1]["duplicate"] is False and again["lines"][1]["probable"] is False
    assert again["lines"][1]["ticked"] is True
    sent = _review({**_HEADER, "closing_balance": "90.00"}, [_line("2026-01-02", "-10.00", "FEE", reference="INV 7",
                                                                   fitid="F1")], known=_held(first, [0]))
    assert bool(sent["lines"][0]["duplicate"]) is True and sent["lines"][0]["reference"] == "INV 7"


def test_a_fitid_match_confirms_a_duplicate_even_when_date_amount_and_description_differ():
    held = _held(_review({**_HEADER, "closing_balance": "90.00"}, [_line("2026-01-02", "-10.00", "FEE", fitid="F1")]))
    again = _review({**_HEADER, "closing_balance": "1.00"},
                     [_line("2026-02-15", "-99.00", "CORRECTED", fitid="F1")], known=held)
    assert bool(again["lines"][0]["duplicate"]) is True


def test_two_duplicates_sharing_one_fitid_keep_the_distinct_fingerprints_of_what_they_repeat():
    lines = [_line("2026-01-02", "-3.00", "ATM FEE", fitid="F1"), _line("2026-01-05", "-10.00", "SHOP", fitid="F1")]
    first = _review({**_HEADER, "closing_balance": "87.00"}, lines)
    again = _review({**_HEADER, "closing_balance": "87.00"}, lines, known=_held(first))
    assert [bool(l["duplicate"]) for l in again["lines"]] == [True, True]
    assert [l["fingerprint"] for l in again["lines"]] == [l["fingerprint"] for l in first["lines"]]


def test_two_identical_lines_against_one_held_leave_one_for_review_and_import_the_other():
    lines = [_line("2026-01-02", "-3.00", "ATM FEE"), _line("2026-01-02", "-3.00", "ATM FEE")]
    held = _held(_review({**_HEADER, "closing_balance": "97.00"}, lines[:1]))
    out = _review({**_HEADER, "closing_balance": "94.00"}, lines, known=held)
    assert [(l["duplicate"], l["probable"], l["ticked"]) for l in out["lines"]] == [(False, True, False),
                                                                                  (False, False, True)]
    fps = [l["fingerprint"] for l in out["lines"]]
    assert len(set(fps)) == 2 and held[0]["fingerprint"] not in fps


def test_a_line_another_format_read_with_balances_is_confirmed_only_where_the_evidence_names_it():
    header = {**_HEADER, "closing_balance": "80.00"}
    csv = _review(header, [_line("2026-01-02", "-10.00", "FEE", "90.00"), _line("2026-01-03", "-10.00", "SHOP", "80.00")])
    ofx = _review(header, [_line("2026-01-02", "-10.00", "FEE", reference="F1", source="ofx"),
                           _line("2026-01-03", "-10.00", "SHOP", reference="F2", source="ofx")], known=_held(csv))
    assert [(l["duplicate"], l["repeats"]) for l in ofx["lines"]] == [(False, "2025-12-31"), (False, "2025-12-31")]
    layout = _review(header, [_line("2026-01-02", "-10.00", "fee", "90.00"), _line("2026-01-03", "-10.00", "SHOP", "80.00")],
                     known=_held(csv))
    assert [bool(l["duplicate"]) for l in layout["lines"]] == [True, True]
    other = _review(header, [_line("2026-01-02", "-10.00", "FEE", "91.00")], known=_held(csv))
    assert other["lines"][0]["duplicate"] is False and other["lines"][0]["probable"] is False


def _again(header, lines, imported: list[int]):
    """``lines`` reviewed again once the lines at ``imported`` were imported by an earlier statement."""
    return _review(header, lines, known=_held(_review(header, lines), imported))


def test_overlap_ties_out_with_the_lines_already_imported():
    header = {**_HEADER, "closing_balance": "130.00"}
    out = _again(header, [_line("2026-01-05", "10.00", "DEPOSIT A", "110.00"),
                          _line("2026-01-06", "20.00", "DEPOSIT B", "130.00")], [0])
    assert out["lines"][0]["duplicate"] and out["lines"][0]["ticked"] is False
    assert out["lines"][1]["ticked"] is True
    assert out["tie"]["ok"] is True and out["blocking"] == []


def test_overlap_never_flips_the_sign_of_a_new_line():
    out = _again({**_HEADER, "closing_balance": "115.00"}, [_line("2026-01-05", "30.00", "DEPOSIT A"),
                                                            _line("2026-01-06", "-15.00", "CARD FEE")], [0])
    assert out["flipped"] is False and out["blocking"] == []
    assert [l["amount"] for l in out["lines"]] == ["30.00", "-15.00"]


def test_a_statement_that_ties_only_without_its_imported_lines_is_blocked():
    # 100 + 10 + 20 is 130, not 120: the header leaves out the line imported before.
    out = _again({**_HEADER, "closing_balance": "120.00"}, [_line("2026-01-05", "10.00", "DEPOSIT A"),
                                                            _line("2026-01-06", "20.00", "DEPOSIT B")], [0])
    assert out["flipped"] is False
    assert [b["code"] for b in out["blocking"]] == ["tie_out"]


def test_overlap_with_repeated_identical_lines_leaves_each_held_one_for_review_once():
    lines = [_line("2026-01-02", "-3.00", "ATM FEE"), _line("2026-01-02", "-3.00", "ATM FEE"),
             _line("2026-01-03", "-3.00", "ATM FEE")]
    out = _again({**_HEADER, "closing_balance": "91.00"}, lines, [0, 1])
    assert [(bool(l["duplicate"]), l["probable"], l["ticked"]) for l in out["lines"]] == [
        (False, True, False), (False, True, False), (False, False, True)]
    assert out["tie"]["ok"] is True and out["blocking"] == []


def test_card_statement_overlap_reverses_every_line_and_keeps_its_fingerprints():
    header = {**_HEADER, "opening_balance": "0.00", "closing_balance": "-100.00"}
    lines = [_line("2026-01-02", "25.00", "COFFEE", "-25.00"), _line("2026-01-05", "75.00", "HOTEL", "-100.00")]
    first = _review(header, lines)
    out = _again(header, lines, [0])
    assert out["flipped"] is True and out["blocking"] == []
    assert [l["amount"] for l in out["lines"]] == ["-25.00", "-75.00"]
    assert [l["fingerprint"] for l in out["lines"]] == [l["fingerprint"] for l in first["lines"]]
    assert out["lines"][0]["duplicate"] and not out["lines"][1]["duplicate"]


def test_pdf_and_csv_overlap_flagged_probable_duplicate():
    lines = [_line("2026-01-02", "-10.00", "Fee charged Jan"), _line("2026-01-04", "-7.00", "Other")]
    out = _review({**_HEADER, "closing_balance": "83.00"}, lines, held_counts={("2026-01-02", "-10.00"): 1})
    assert out["lines"][0]["probable"] is True and out["lines"][0]["ticked"] is False
    assert out["lines"][0]["repeats"] is None
    assert out["lines"][1]["probable"] is False and out["lines"][1]["ticked"] is True


def test_two_identical_lines_against_one_posted_entry_leave_only_one_probable():
    lines = [_line("2026-01-02", "-10.00", "Fee charged Jan"), _line("2026-01-02", "-10.00", "Fee charged Jan")]
    out = _review({**_HEADER, "closing_balance": "80.00"}, lines, held_counts={("2026-01-02", "-10.00"): 1})
    assert out["lines"][0]["probable"] is True and out["lines"][0]["ticked"] is False
    assert out["lines"][1]["probable"] is False and out["lines"][1]["ticked"] is True


_OFX_SGML = b"""OFXHEADER:100
DATA:OFXSGML
<!DOCTYPE x [<!ENTITY boom "EXPANDED">]>
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKTRANLIST><DTSTART>20260101<DTEND>20260131
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260105<TRNAMT>-12.50<FITID>A1<NAME>&boom;<MEMO>Coffee</STMTTRN>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260107120000<TRNAMT>200.00<FITID>A2<NAME>ACME LTD</STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>287.50<DTASOF>20260131</LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""

_OFX_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="220"?>
<!DOCTYPE OFX [<!ENTITY boom "EXPANDED">]>
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD</CURDEF>
<BANKTRANLIST><DTSTART>20260101</DTSTART><DTEND>20260131</DTEND>
<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260105</DTPOSTED><TRNAMT>-12.50</TRNAMT><FITID>A1</FITID><NAME>&boom;</NAME></STMTTRN>
<STMTTRN><TRNTYPE>XFER</TRNTYPE><DTPOSTED>20260107</DTPOSTED><TRNAMT>200.00</TRNAMT><FITID>A2</FITID><NAME>ACME LTD</NAME><BANKACCTTO><ACCTID>998877665544</ACCTID></BANKACCTTO></STMTTRN>
</BANKTRANLIST><LEDGERBAL><BALAMT>287.50</BALAMT></LEDGERBAL></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>
"""


@pytest.mark.parametrize("content", [_OFX_SGML, _OFX_XML], ids=["sgml", "xml"])
def test_ofx_sgml_and_xml_parse_without_entities(content):
    out = parse_ofx(content)
    assert out["period_start"] == "2026-01-01" and out["period_end"] == "2026-01-31"
    assert out["closing_balance"] == "287.50" and out["currency"] == "USD"
    assert [(l["line_date"], l["amount"], l["reference"]) for l in out["lines"]] == [
        ("2026-01-05", "-12.50", "A1"), ("2026-01-07", "200.00", "A2")]
    assert "EXPANDED" not in repr(out)
    assert out["lines"][0]["description"] == "&boom;" or "boom" in out["lines"][0]["description"]


def test_ofx_fitid_is_the_fingerprint_source():
    lines = parse_ofx(_OFX_XML)["lines"]
    a = _review({**_HEADER, "opening_balance": "100.00", "closing_balance": "287.50"}, lines)
    b = _review({**_HEADER, "opening_balance": "100.00", "closing_balance": "287.50"},
                [{**lines[0], "description": "renamed"}, lines[1]])
    assert [l["fingerprint"] for l in a["lines"]] == [l["fingerprint"] for l in b["lines"]]
    assert lines[1]["counterparty_account"] == "998877665544"


# Description, reference, amount -> (key, account)
_KEYS = [
    ("POS 12/01 STARBUCKS #1234 BANGKOK", None, "-4.50", ("STARBUCKS BANGKOK", None)),
    ("POS 15/01 STARBUCKS #9981 BANGKOK", None, "-6.00", ("STARBUCKS BANGKOK", None)),
    ("TRANSFER TO 1234567890 SOMCHAI", None, "-500", ("ACCT:1234567890", "1234567890")),
    ("TRANSFER FROM 9876543210", None, "800", ("ACCT:9876543210", "9876543210")),
    ("PROMPTPAY 0812345678 NOODLE SHOP", None, "-80", ("ACCT:0812345678", "0812345678")),
    ("ATM WITHDRAWAL 05/01 12:30", None, "-1000", ("SPARSE:ATM:out", None)),
    ("WITHDRAWAL", None, "-200", ("SPARSE:WITHDRAWAL:out", None)),
    ("TRANSFER", None, "300", ("SPARSE:TRANSFER:in", None)),
    ("INV-2026-0042", "INV-2026-0042", "1500", ("SPARSE:REFERENCE:in", None)),
    ("ACME LTD INV-2026-0042", "INV-2026-0042", "1500", ("ACME LTD", None)),
    ("AMAZON WEB SERVICES AWS.AMAZON.CO", None, "-120", ("AMAZON WEB SERVICES", None)),
    ("AMAZON WEB SERVICES 2026-01-03", None, "-99", ("AMAZON WEB SERVICES", None)),
    ("CARD 4111********1111 GRAB *TRIP", None, "-150", ("GRAB TRIP", None)),
    ("CARD 5500********0004 GRAB *TRIP", None, "-90", ("GRAB TRIP", None)),
    ("SALARY JAN 2026 ACME LTD", None, "3000", ("SALARY JAN ACME", None)),
    ("DIRECT DEBIT TRUE MOVE H", None, "-599", ("DIRECT DEBIT TRUE", None)),
    ("BANK FEE", None, "-25", ("BANK FEE", None)),
    ("MONTHLY SERVICE FEE", None, "-10", ("MONTHLY SERVICE FEE", None)),
    ("INTEREST CREDIT", None, "1.25", ("INTEREST CREDIT", None)),
    ("โอนเงิน 1234567890 สมชาย", None, "-700", ("ACCT:1234567890", "1234567890")),
    ("ถอนเงินสด ATM", None, "-2000", ("SPARSE:ATM:out", None)),
    ("ถอนเงิน", None, "-100", ("SPARSE:WITHDRAWAL:out", None)),
    ("ค่าธรรมเนียม", None, "-15", ("ค่าธรรมเนียม", None)),
    ("ชำระเงิน 7-ELEVEN สาขา 1234", None, "-45", ("7-ELEVEN สาขา", None)),
    ("รับโอน 5556667778", None, "900", ("ACCT:5556667778", "5556667778")),
    ("PAYPAL *SPOTIFY 402-935-7733", None, "-11.99", ("PAYPAL SPOTIFY", None)),
    ("CHQ 000123", None, "-400", ("SPARSE:CHEQUE:out", None)),
    ("DEPOSIT", None, "50", ("SPARSE:DEPOSIT:in", None)),
    ("REF 88231 UBER EATS", "88231", "-23", ("UBER EATS", None)),
    ("  uber   eats  ", None, "-17", ("UBER EATS", None)),
]


@pytest.mark.parametrize("desc,ref,amount,expected", _KEYS, ids=[k[0][:24] for k in _KEYS])
def test_counterparty_key_table(desc, ref, amount, expected):
    assert counterparty_key(desc, ref, Decimal(amount)) == expected


@pytest.mark.parametrize("bad", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity", "1e309"])
def test_a_balance_or_amount_that_is_no_number_is_unread_and_blocks(bad):
    """NaN and infinity parse as decimals; they are no amount, so the review reads them as
    missing and blocks the import, never compares them (a 500 before). A closing balance read
    as missing falls back to the last running balance, so that case carries none."""
    lines = [_line("2026-01-02", "-10.00", balance="90.00")]
    for header, body in (({**_HEADER, "opening_balance": bad, "closing_balance": "90.00"}, lines),
                         ({**_HEADER, "closing_balance": bad}, [_line("2026-01-02", "-10.00")]),
                         ({**_HEADER, "closing_balance": "90.00"}, [_line("2026-01-02", bad)])):
        out = _review(header, body)
        assert out["blocking"] and out["tie"]["ok"] is not True, (header, body)
        assert all(l["amount"] is None or Decimal(l["amount"]).is_finite() for l in out["lines"])
    # A running balance is optional: one that is no number reads as none.
    out = _review({**_HEADER, "closing_balance": "90.00"}, [_line("2026-01-02", "-10.00", balance=bad)])
    assert out["lines"][0]["raw_balance"] is None


# The sign convention is decided first, from the opening, the closing and every running balance;
# every line's identity is then read from the amounts in the bank's convention.
_CARD = {**_HEADER, "opening_balance": "0.00", "closing_balance": "-100.00"}


def test_a_reversed_card_statement_with_running_balances_imports_reversed():
    out = _review(_CARD, [_line("2026-01-02", "25.00", "COFFEE", "-25.00"),
                          _line("2026-01-05", "75.00", "HOTEL", "-100.00")])
    assert out["flipped"] is True and out["blocking"] == []
    assert out["tie"] == {"ok": True, "first_break": None}
    assert [l["amount"] for l in out["lines"]] == ["-25.00", "-75.00"]


def test_a_reversed_line_without_a_counterparty_is_keyed_as_spending():
    out = _review(_CARD, [_line("2026-01-02", "100.00", "ATM", "-100.00")])
    assert out["flipped"] is True and out["lines"][0]["counterparty_key"] == "SPARSE:ATM:out"


def test_a_line_imported_from_another_format_is_a_duplicate_on_a_reversed_statement():
    bank = _review(_CARD, [_line("2026-01-05", "-100.00", "HOTEL", "-100.00")])
    card = _review(_CARD, [_line("2026-01-05", "100.00", "HOTEL", "-100.00")], known=_held(bank))
    assert card["flipped"] is True and card["lines"][0]["fingerprint"] == bank["lines"][0]["fingerprint"]
    assert card["lines"][0]["duplicate"] == t("acct.recon_duplicate_of", date="2025-12-31")
    assert card["lines"][0]["ticked"] is False
    held = _review(_CARD, [_line("2026-01-05", "100.00", "HOTEL")],
                   held_counts={("2026-01-05", "-100.00"): 1})
    assert held["lines"][0]["probable"] is True


def test_repeated_identical_lines_of_a_reversed_statement_keep_their_fingerprints_across_imports():
    lines = [_line("2026-01-02", "50.00", "ATM FEE"), _line("2026-01-02", "50.00", "ATM FEE")]
    first, again = _again(_CARD, lines, []), _again(_CARD, lines, [0, 1])
    bank = _review(_CARD, [_line("2026-01-02", "-50.00", "ATM FEE"), _line("2026-01-02", "-50.00", "ATM FEE")])
    fps = [l["fingerprint"] for l in first["lines"]]
    assert fps[0] != fps[1] and fps == [l["fingerprint"] for l in bank["lines"]]
    assert [(l["probable"], l["ticked"]) for l in again["lines"]] == [(True, False), (True, False)]
    assert not set(fps) & {l["fingerprint"] for l in again["lines"]}


def test_a_statement_that_ties_in_neither_convention_blocks_at_the_first_break_as_read():
    out = _review({**_HEADER, "closing_balance": "85.00"}, [_line("2026-01-02", "-10.00", "A", "90.00"),
                                                           _line("2026-01-03", "-5.00", "B", "80.00")])
    assert out["flipped"] is False and [l["amount"] for l in out["lines"]] == ["-10.00", "-5.00"]
    assert [(b["code"], b["line"]) for b in out["blocking"]] == [("tie_break", 1)]


def test_a_refund_is_never_the_duplicate_of_the_charge_it_mirrors():
    charge = _line("2026-01-02", "-100.00", "TRANSFER SHOP")
    first = _review({**_HEADER, "closing_balance": "0.00"}, [charge])
    out = _review({**_HEADER, "closing_balance": "100.00"}, [_line("2026-01-02", "100.00", "TRANSFER SHOP"), charge],
                  known=_held(first))
    assert out["flipped"] is False and [l["probable"] for l in out["lines"]] == [False, True]
