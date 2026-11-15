# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Tests for Phase 9 reconciliation: CSV parser, matcher, API endpoints, rules."""

from __future__ import annotations

import uuid as _uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient

import bank_statement_helpers as B


# ── Auth helper ───────────────────────────────────────────────────────────────

async def _auth(client: AsyncClient, suffix: str = "") -> dict:
    uid = suffix or _uuid.uuid4().hex[:8]
    r = await client.post("/auth/register", json={
        "email": f"recon_{uid}@example.com",
        "password": "pass1234",
        "name": "Recon Tester",
        "company_name": f"ReconCo_{uid}",
    })
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _create_bank(client: AsyncClient, headers: dict) -> dict:
    r = await client.post("/accounting/bank-accounts", json={
        "bank_name": "Test Bank",
        "account_number": "****9999",
        "bank_type": "checking",
        "currency": "USD",
        "opening_balance": 100000.0,
    }, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _create_recon(client: AsyncClient, headers: dict, bank_id: str) -> dict:
    r = await client.post("/accounting/reconciliation/start", json={
        "bank_account_id": bank_id,
        "statement_date": "2026-03-31",
        "statement_balance": 105000.0,
    }, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _import_csv(client: AsyncClient, headers: dict, sid: str, csv_bytes: bytes, fname: str = "test.csv") -> dict:
    """The statement ``sid`` stands for, read from ``csv_bytes`` and imported on its bank."""
    return await B.import_into(client, headers, sid, csv_bytes, fname)


async def _one_line(client: AsyncClient, headers: dict, bank: dict, csv_bytes: bytes) -> tuple[str, str]:
    """A March statement on ``bank`` with one line: its session id and line id."""
    recon = await _create_recon(client, headers, bank["id"])
    data = await _import_csv(client, headers, recon["id"], csv_bytes)
    return data["session_id"], data["lines"][0]["id"]


async def _line(client: AsyncClient, headers: dict, sid: str, line_id: str) -> dict:
    items = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=headers)).json()["items"]
    return next(item for item in items if item["id"] == line_id)


# ── CSV parser tests (pure, no DB) ───────────────────────────────────────────


class TestCsvParser:

    @pytest.fixture
    def parse(self):
        from celerp_accounting.csv_parser import parse_bank_csv
        return parse_bank_csv

    def test_auto_detect_standard_headers(self, parse):
        csv = b"Date,Description,Debit,Credit,Balance\n2026-03-01,Wire ACME,45000,,1200000\n"
        result = parse(csv)
        assert not result["needs_mapping"]
        assert len(result["lines"]) == 1
        line = result["lines"][0]
        assert line["line_date"] == "2026-03-01"
        assert line["description"] == "Wire ACME"
        assert Decimal(line["amount"]) == Decimal("-45000")

    def test_auto_detect_amount_column(self, parse):
        csv = b"Date,Transaction,Amount,Balance\n2026-03-02,Deposit,5000.50,1205000\n"
        result = parse(csv)
        assert not result["needs_mapping"]
        assert Decimal(result["lines"][0]["amount"]) == Decimal("5000.50")

    def test_needs_mapping_when_ambiguous(self, parse):
        csv = b"Col A,Col B,Col C\n2026-03-01,Something,1000\n"
        result = parse(csv)
        assert result["needs_mapping"]
        assert result["headers"] == ["Col A", "Col B", "Col C"]
        assert len(result["lines"]) == 0
        assert len(result["preview"]) == 1

    def test_explicit_column_map(self, parse):
        csv = b"Col A,Col B,Col C\n2026-03-01,Rent payment,5000\n"
        result = parse(csv, column_map={"date": "Col A", "description": "Col B", "amount": "Col C"})
        assert not result["needs_mapping"]
        assert result["lines"][0]["description"] == "Rent payment"

    def test_handles_bom_utf8(self, parse):
        csv = b"\xef\xbb\xbfDate,Description,Amount\n2026-03-01,Test,100\n"
        result = parse(csv)
        assert not result["needs_mapping"]
        assert len(result["lines"]) == 1

    def test_empty_csv(self, parse):
        csv = b"Date,Description,Amount\n"
        result = parse(csv)
        assert len(result["lines"]) == 0

    def test_parentheses_negative(self, parse):
        csv = b"Date,Description,Amount\n2026-03-01,Refund,(500)\n"
        result = parse(csv)
        assert Decimal(result["lines"][0]["amount"]) == Decimal("-500")

    def test_debit_credit_columns(self, parse):
        csv = (
            b"Date,Description,Withdrawals,Deposits,Balance\n"
            b"2026-03-01,Wire out,10000,,90000\n"
            b"2026-03-02,Deposit,,5000,95000\n"
        )
        result = parse(csv)
        assert not result["needs_mapping"]
        assert Decimal(result["lines"][0]["amount"]) == Decimal("-10000")
        assert Decimal(result["lines"][1]["amount"]) == Decimal("5000")

    def test_preview_always_populated(self, parse):
        csv = b"Col X,Col Y\nfoo,bar\nbaz,qux\n"
        result = parse(csv)
        assert result["needs_mapping"]
        assert len(result["preview"]) == 2

    def test_raw_csv_row_preserved(self, parse):
        csv = b"Date,Description,Amount,Reference\n2026-03-01,Test,100,REF123\n"
        result = parse(csv)
        line = result["lines"][0]
        assert line["raw_csv_row"]["Reference"] == "REF123"
        assert line["reference"] == "REF123"


# ── Matcher tests (pure, no DB) ──────────────────────────────────────────────


class TestAutoMatch:

    @pytest.fixture
    def match(self):
        from celerp_accounting.matcher import auto_match
        return auto_match

    def _line(self, *, lid="line-1", dt="2026-03-01", desc="Wire ACME", amt=-45000):
        return {"id": lid, "line_date": dt, "description": desc, "amount": amt, "status": "unmatched"}

    def _entry(self, *, eid="je:001", ts="2026-03-01", memo="ACME payment", amt=-45000):
        return {"je_id": eid, "ts": ts, "memo": memo, "amount": amt}

    def test_exact_amount_date(self, match):
        results = match([self._line()], [self._entry()])
        assert len(results) == 1
        assert results[0] == ("line-1", "je:001", "high")

    def test_exact_amount_3_day_window(self, match):
        results = match([self._line(dt="2026-03-04")], [self._entry(ts="2026-03-01")])
        assert len(results) == 1
        assert results[0][2] == "high"

    def test_exact_amount_7_day_window(self, match):
        results = match([self._line(dt="2026-03-08")], [self._entry(ts="2026-03-01")])
        assert len(results) == 1
        assert results[0][2] == "medium"

    def test_no_match_outside_window(self, match):
        results = match([self._line(dt="2026-03-15")], [self._entry(ts="2026-03-01")])
        assert len(results) == 0

    def test_reference_match(self, match):
        """A reference the line names is strong evidence only when the amounts agree."""
        results = match(
            [self._line(desc="Payment ref INV-0042", amt=-200, dt="2026-03-20")],
            [self._entry(memo="INV-0042 shipped", amt=-200, ts="2026-03-01")],
        )
        assert [r[2] for r in results] == ["high"]
        results = match(
            [self._line(desc="Payment ref INV-0042", amt=-100)],
            [self._entry(memo="INV-0042 shipped", amt=-200, ts="2026-03-01")],
        )
        assert [r[2] for r in results] == ["low"]

    def test_tolerance_name_match(self, match):
        results = match(
            [self._line(desc="ACME Corp wire", amt=-10050)],
            [self._entry(memo="ACME Corp payment", amt=-10000, ts="2026-03-01")],
            tolerance_pct=0.02,
        )
        assert len(results) == 1
        assert results[0][2] == "medium"

    def test_one_entry_matched_once(self, match):
        lines = [self._line(lid="a", amt=-100), self._line(lid="b", amt=-100)]
        entries = [self._entry(eid="je:x", amt=-100)]
        results = match(lines, entries)
        assert len(results) == 1

    def test_skips_non_unmatched_lines(self, match):
        lines = [{"id": "x", "line_date": "2026-03-01", "description": "test", "amount": -100, "status": "matched"}]
        results = match(lines, [self._entry(amt=-100)])
        assert len(results) == 0


class TestApplyRules:

    @pytest.fixture
    def apply(self):
        from celerp_accounting.matcher import apply_rules
        return apply_rules

    def test_contains_match(self, apply):
        lines = [{"id": "l1", "description": "Monthly POS Terminal Fee", "status": "unmatched"}]
        rules = [{"id": "r1", "match_field": "description", "match_pattern": "POS Terminal",
                  "match_type": "contains", "is_active": True}]
        results = apply(lines, rules)
        assert results == [("l1", "r1")]

    def test_exact_match(self, apply):
        lines = [{"id": "l1", "description": "Bank Service Charge", "status": "unmatched"}]
        rules = [{"id": "r1", "match_field": "description", "match_pattern": "Bank Service Charge",
                  "match_type": "exact", "is_active": True}]
        assert len(apply(lines, rules)) == 1

    def test_starts_with_match(self, apply):
        lines = [{"id": "l1", "description": "VISA PURCHASE 12345", "status": "unmatched"}]
        rules = [{"id": "r1", "match_field": "description", "match_pattern": "VISA PURCHASE",
                  "match_type": "starts_with", "is_active": True}]
        assert len(apply(lines, rules)) == 1

    def test_inactive_rule_skipped(self, apply):
        lines = [{"id": "l1", "description": "POS Terminal Fee", "status": "unmatched"}]
        rules = [{"id": "r1", "match_field": "description", "match_pattern": "POS Terminal",
                  "match_type": "contains", "is_active": False}]
        assert len(apply(lines, rules)) == 0

    def test_first_rule_wins(self, apply):
        lines = [{"id": "l1", "description": "POS Terminal Fee", "status": "unmatched"}]
        rules = [
            {"id": "r1", "match_field": "description", "match_pattern": "POS", "match_type": "contains", "is_active": True},
            {"id": "r2", "match_field": "description", "match_pattern": "Terminal", "match_type": "contains", "is_active": True},
        ]
        results = apply(lines, rules)
        assert results == [("l1", "r1")]

    def test_already_matched_skipped(self, apply):
        lines = [{"id": "l1", "description": "POS Fee", "status": "matched"}]
        rules = [{"id": "r1", "match_field": "description", "match_pattern": "POS", "match_type": "contains", "is_active": True}]
        assert len(apply(lines, rules)) == 0


# ── API endpoint tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_csv_auto_detect(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    data = await _import_csv(
        client, h, recon["id"],
        b"Date,Description,Amount,Balance\n2026-03-01,Wire ACME,-5000,95000\n2026-03-05,Deposit,10000,105000\n",
        "statement.csv",
    )
    assert [(line["description"], line["amount"]) for line in data["lines"]] == [
        ("Wire ACME", "-5000.00"), ("Deposit", "10000.00")]


@pytest.mark.asyncio
@pytest.mark.parametrize("csv_bytes", [
    b"Date,Description,Amount,Amount\n2026-01-01,x,-5000,123\n",
    b"Date,Description,Amount,\n2026-01-01,x,-5000,123\n",
], ids=["duplicate_header", "blank_header"])
async def test_import_csv_refuses_a_header_that_would_lose_values(client, csv_bytes):
    """Two columns under one header, or a filled column with none, are refused
    naming the column; no statement is read from a collapsed row."""
    h = await _auth(client)
    bank = await _create_bank(client, h)
    preview = await B.read(client, await B.co_of(client, h), [(csv_bytes, "dup.csv", "text/csv")], bank_id=bank["id"])
    assert preview["statements"] == [], preview
    (failed,) = preview["failed_steps"]
    assert " D " in failed["reason"] or " D has" in failed["reason"], failed


@pytest.mark.asyncio
async def test_get_statement_lines(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, _ = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Test line,-1000\n")
    r = await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["description"] == "Test line"
    assert items[0]["status"] == "unmatched"


@pytest.mark.asyncio
async def test_suggest_endpoint_suggests_and_posts_nothing(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, line_id = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Payment,-1000\n")
    r = await B.bank_post(client, h, sid, "suggest")
    assert r.status_code == 200, r.text
    line = await _line(client, h, sid, line_id)
    assert (line["status"], line["matched_je_ids"]) == ("unmatched", [])


@pytest.mark.asyncio
async def test_match_and_unmatch_line(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, line_id = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Match test,-500\n")

    r = await client.post("/accounting/journal-entries", headers=h, json={
        "ts": "2026-03-01", "memo": "Match test", "idempotency_token": _uuid.uuid4().hex,
        "entries": [{"account": "5100", "debit": 500.0, "credit": 0.0},
                    {"account": bank["chart_account_code"], "debit": 0.0, "credit": 500.0}]})
    assert r.status_code == 200, r.text
    je_id = r.json()["je_id"]

    r = await B.decide(client, h, sid, line_id, "match", je_ids=[je_id])
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    line = await _line(client, h, sid, line_id)
    assert (line["status"], line["matched_je_ids"]) == ("matched", [je_id])

    r = await B.undo(client, h, sid, [line_id])
    assert r.status_code == 200, r.text
    line = await _line(client, h, sid, line_id)
    assert (line["status"], line["matched_je_ids"]) == ("unmatched", [])


@pytest.mark.asyncio
async def test_skip_line(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, line_id = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Skip me,-100\n")
    r = await B.decide(client, h, sid, line_id, "no_entry")
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    assert (await _line(client, h, sid, line_id))["status"] == "skipped"


@pytest.mark.asyncio
async def test_create_je_from_line(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, line_id = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Bank Fee,-500\n")
    r = await B.decide(client, h, sid, line_id, "post", account_code="6100", memo="Monthly bank fee")
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    line = await _line(client, h, sid, line_id)
    assert line["status"] == "posted" and len(line["matched_je_ids"]) == 1


@pytest.mark.asyncio
async def test_split_line(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, line_id = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Mixed,-1500\n")
    r = await B.decide(client, h, sid, line_id, "post", splits=[
        {"account_code": "5100", "amount": 1000}, {"account_code": "6300", "amount": 500}])
    assert r.status_code == 200 and r.json()["results"][0]["ok"], r.text
    assert (await _line(client, h, sid, line_id))["status"] == "posted"


@pytest.mark.asyncio
async def test_completed_statement_takes_no_more_lines(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    sid, line_id = await _one_line(client, h, bank, b"Date,Description,Amount\n2026-03-01,Sale,5000\n")
    assert (await B.decide(client, h, sid, line_id, "post", account_code="4100")).json()["results"][0]["ok"]
    r = await B.bank_post(client, h, sid, "complete")
    assert r.status_code == 200 and r.json()["stopped_at"] is None, r.text
    co = await B.co_of(client, h)
    preview = await B.read(client, co, [(b"Date,Description,Amount\n2026-03-01,Late,-100\n", "late.csv", "text/csv")],
                           bank_id=bank["id"])
    r = await client.post(f"{B.API}/import-statement", headers=h, json={
        "job_id": preview["job_id"], "bank_id": bank["id"], "idempotency_key": B.key(), "statements": [{
            "header": {**preview["statements"][0]["header"], "opening_balance": "105000.00",
                       "closing_balance": "104900.00", "period_start": "2026-03-01", "period_end": "2026-03-31"},
            "lines": [{**line, "ticked": True} for line in preview["statements"][0]["lines"]]}]})
    assert r.status_code in (409, 422), r.text
    items = (await client.get(f"/accounting/reconciliation/{sid}/statement-lines", headers=h)).json()["items"]
    assert [item["description"] for item in items] == ["Sale"]


# ── Rules API tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rules_crud(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)

    # Create
    r = await client.post("/accounting/rules", json={
        "bank_account_id": bank["id"],
        "match_pattern": "POS Terminal Fee",
        "match_type": "contains",
        "target_account_code": "6100",
        "default_memo": "Monthly POS fee",
    }, headers=h)
    assert r.status_code == 200
    rule = r.json()
    assert rule["match_pattern"] == "POS Terminal Fee"
    assert rule["is_active"]
    rule_id = rule["id"]

    # List
    r = await client.get(f"/accounting/rules?bank_account_id={bank['id']}", headers=h)
    assert r.status_code == 200
    assert any(item["id"] == rule_id for item in r.json()["items"])

    # Patch
    r = await client.patch(f"/accounting/rules/{rule_id}", json={
        "match_pattern": "Updated Pattern",
        "is_active": False,
    }, headers=h)
    assert r.status_code == 200
    assert r.json()["match_pattern"] == "Updated Pattern"
    assert not r.json()["is_active"]

    # Delete
    r = await client.delete(f"/accounting/rules/{rule_id}", headers=h)
    assert r.status_code == 200
    r = await client.get(f"/accounting/rules?bank_account_id={bank['id']}", headers=h)
    assert not any(item["id"] == rule_id for item in r.json()["items"])


@pytest.mark.asyncio
async def test_write_off(client):
    h = await _auth(client)
    bank = await _create_bank(client, h)
    recon = await _create_recon(client, h, bank["id"])
    r = await client.post(
        f"/accounting/reconciliation/{recon['id']}/write-off",
        json={"adjustment_account": "6950"}, headers=h,
    )
    assert r.status_code in (200, 422)
