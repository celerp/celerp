# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""The statement reader: OFX, tie-out, sign convention, flagged rows, period, currency,
line identity and the counterparty key a group is built on. Pure functions, no database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from celerp_accounting.csv_parser import parse_bank_csv, parse_ofx
from celerp_accounting.statement_reader import counterparty_key, review_statement

BANK = "11111111-1111-1111-1111-111111111111"


def _line(d, amount, desc="Line", balance=None, reference=None):
    return {"line_date": d, "description": desc, "amount": amount, "raw_balance": balance, "reference": reference}


def _review(header, lines, **kw):
    kw.setdefault("bank_currency", "USD")
    kw.setdefault("bank_id", BANK)
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


def test_existing_fingerprint_listed_and_unticked():
    lines = [_line("2026-01-02", "-10.00", "FEE")]
    first = _review({**_HEADER, "closing_balance": "90.00"}, lines)
    fp = first["lines"][0]["fingerprint"]
    again = _review({**_HEADER, "closing_balance": "90.00"}, lines, existing={fp: "Statement 2026-01-31"})
    line = again["lines"][0]
    assert line["duplicate"] == "Statement 2026-01-31" and line["ticked"] is False


def test_pdf_and_csv_overlap_flagged_probable_duplicate():
    lines = [_line("2026-01-02", "-10.00", "Fee charged Jan"), _line("2026-01-04", "-7.00", "Other")]
    out = _review({**_HEADER, "closing_balance": "83.00"}, lines, held_counts={("2026-01-02", "-10.00"): 1})
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
