# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company copy of the demo company, opened on a fresh installation, has the same
trial balance, receivables and payables as the company it was taken from."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from company_backup_support import download
from migration_support import auth, real_client, real_engine  # noqa: F401
from test_company_backup_lifecycle import _data_dir, _fresh_install  # noqa: F401

pytestmark = pytest.mark.asyncio

_SEED_PATH = Path(__file__).resolve().parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_round_trip", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)


def _aging(body: dict) -> tuple:
    names = [next(v for k, v in line.items() if k.endswith("_name")) for line in body["lines"]]
    return ({k: round(v, 2) for k, v in body["buckets"].items()},
            sorted((name, round(line["total"], 2)) for name, line in zip(names, body["lines"])))


async def _books(client, tok: str) -> dict:
    """Trial balance per account, and receivables and payables per contact and bucket."""
    tb = await client.get("/accounting/trial-balance", headers=auth(tok))
    ar = await client.get("/reports/ar-aging", headers=auth(tok))
    ap = await client.get("/reports/ap-aging", headers=auth(tok))
    for r in (tb, ar, ap):
        assert r.status_code == 200, r.text
    return {
        "trial_balance": {line["code"]: (round(line["total_debit"], 2), round(line["total_credit"], 2))
                          for line in tb.json()["lines"] if line["total_debit"] or line["total_credit"]},
        "receivables": _aging(ar.json()),
        "payables": _aging(ap.json()),
    }


async def test_round_trip_ties_out_tb_ar_ap(real_client, real_engine):
    registered = await real_client.post("/auth/register", json={
        "company_name": "Demo Trading", "email": "demo-owner@example.test",
        "name": "Demo Owner", "password": "demopw1234"})
    assert registered.status_code == 200, registered.text
    tok = registered.json()["access_token"]
    await seed_demo.seed_items(real_client, tok)
    contacts = await seed_demo.seed_contacts(real_client, tok)
    await seed_demo.seed_docs(real_client, tok, contacts, "invoice", 8)
    await seed_demo.seed_docs(real_client, tok, contacts, "purchase_order", 5)
    before = await _books(real_client, tok)
    # The demo company has real books to compare: postings, money owed both ways.
    assert len(before["trial_balance"]) >= 3
    assert any(before["receivables"][0].values()) and any(before["payables"][0].values())

    data = await download(real_client, tok)
    await _fresh_install(real_engine)
    read = await real_client.post("/company-backups/bootstrap/read",
                                  files={"file": ("demo.celerp-company", data)})
    assert read.status_code == 200, read.text
    opened = await real_client.post("/company-backups/bootstrap/restore", json={
        "upload_token": read.json()["upload_token"], "name": "First Owner",
        "email": "first@example.test", "password": "firstpw1234"})
    assert opened.status_code == 201, opened.text
    login = await real_client.post("/auth/login", json={"email": "first@example.test", "password": "firstpw1234"})
    assert login.status_code == 200, login.text
    after = await _books(real_client, login.json()["access_token"])
    assert after == before
