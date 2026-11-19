# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A database from a released version, on either encoding Celerp creates, upgrades its tax
codes without moving its books. A bill that withheld through a negative rate owes what it owed
and every posted entry keeps its figures; a company whose list came from a country's seed keeps
its names and rates; every code gains an id, rate versions and a category key; an item's tax
code name that matches one code now holds that code's id, and one that matches none is kept."""
from __future__ import annotations

import json
import uuid
from collections import defaultdict

import pytest

from celerp.cli import _migrate_to_head
from .conftest import throwaway_db, upgrade_to
from .test_r1s2t3u4v5w6_projections_canonical_number_lookup import ENCODINGS
from .test_s2t3u4v5w6x7_json_text_utf8 import RELEASED_HEAD, _sql

_OLD = {"taxes": [{"name": "VAT 7%", "rate": 7.0, "tax_type": "sales", "is_default": True, "description": ""}],
        "purchasing_taxes": [
            {"name": "Input VAT 7%", "rate": 7.0, "tax_type": "purchase", "is_default": True, "description": ""},
            {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False, "description": ""}]}
_PT = [{"name": "IVA 23%", "rate": 23.0, "tax_type": "sales", "is_default": True, "description": ""},
       {"name": "IVA 13%", "rate": 13.0, "tax_type": "sales", "is_default": False, "description": ""},
       {"name": "IVA 6%", "rate": 6.0, "tax_type": "sales", "is_default": False, "description": ""}]


def _tax(name: str, rate: float) -> dict:
    return {"code": name, "rate": rate, "amount": round(10 * rate, 2), "order": 0, "is_compound": False,
            "label": name}


def _entry(*lines: tuple) -> dict:
    return {"status": "posted", "entry_date": "2026-10-01",
            "entries": [{"account": a, "account_roles": [r], "debit": d, "credit": c} for a, r, d, c in lines]}


_ROWS = {  # entity id: (type, state)
    "doc:OLD-BILL": ("doc", {"doc_type": "bill", "status": "final", "ref_id": "OLD-BILL", "subtotal": 1000.0,
                             "tax": 40.0, "total": 1040.0, "amount_paid": 0.0, "amount_outstanding": 1040.0,
                             "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0,
                                             "line_total": 1000.0,
                                             "taxes": [_tax("Input VAT 7%", 7.0), _tax("WHT 3%", -3.0)]}]}),
    "doc:OLD-INV": ("doc", {"doc_type": "invoice", "status": "final", "ref_id": "OLD-INV", "subtotal": 1000.0,
                            "tax": 70.0, "total": 1070.0, "amount_paid": 0.0, "amount_outstanding": 1070.0,
                            "line_items": [{"description": "Service", "quantity": 1, "unit_price": 1000.0,
                                            "line_total": 1000.0, "taxes": [_tax("VAT 7%", 7.0)]}]}),
    "je:auto:doc:OLD-BILL:fin": ("journal_entry", _entry(("6950", "general_expense", 1000.0, 0.0),
                                                         ("1150", "tax_input", 40.0, 0.0),
                                                         ("2110", "payable", 0.0, 1040.0))),
    "je:auto:doc:OLD-INV:fin": ("journal_entry", _entry(("1120", "receivable", 1070.0, 0.0),
                                                        ("4100", "sales_revenue", 0.0, 1000.0),
                                                        ("2120", "tax_output", 0.0, 70.0))),
    "item:known": ("item", {"sku": "KNOWN", "name": "Known", "tax_codes": ["VAT 7%"]}),
    "item:gone": ("item", {"sku": "GONE", "name": "Gone", "tax_codes": ["Gone 9%"]}),
}


def _seed(sync_url: str) -> tuple[str, str]:
    ids = []
    for slug, settings in (("old", _OLD), ("pt", {"taxes": _PT, "purchasing_taxes": [], "taxes_seeded_for": "PT"})):
        ids.append(str(_sql(sync_url, "INSERT INTO companies (id, name, slug, settings, created_at) VALUES "
                                      "(gen_random_uuid(), :n, :n, CAST(:s AS json), now()) RETURNING id",
                            {"n": f"{slug}-{uuid.uuid4().hex[:6]}", "s": json.dumps(settings)})[0][0]))
    for entity_id, (kind, state) in _ROWS.items():
        _sql(sync_url, "INSERT INTO projections (company_id, entity_id, entity_type, state, version, updated_at) "
                       "VALUES (CAST(:c AS uuid), :e, :t, CAST(:s AS json), 1, now())",
             {"c": ids[0], "e": entity_id, "t": kind, "s": json.dumps(state)})
    return ids[0], ids[1]


def _states(sync_url: str, cid: str, kind: str) -> dict:
    return {e: json.loads(s) for e, s in _sql(
        sync_url, "SELECT entity_id, CAST(state AS text) FROM projections "
                  "WHERE company_id = CAST(:c AS uuid) AND entity_type = :t", {"c": cid, "t": kind})}


def _trial_balance(sync_url: str, cid: str) -> dict:
    net = defaultdict(float)
    for state in _states(sync_url, cid, "journal_entry").values():
        if state.get("status") == "posted":
            for e in state["entries"]:
                net[e["account"]] += float(e.get("debit") or 0) - float(e.get("credit") or 0)
    return {k: round(v, 2) for k, v in net.items()}


def _settings(sync_url: str, cid: str) -> dict:
    return json.loads(_sql(sync_url, "SELECT CAST(settings AS text) FROM companies WHERE id = CAST(:c AS uuid)",
                           {"c": cid})[0][0])


@pytest.mark.parametrize("encoding", ENCODINGS)
@pytest.mark.parametrize("start", [RELEASED_HEAD, "c4d1e7a2f9b3"])
def test_upgrade_from_released_heads_sql_ascii_utf8(encoding, start):
    with throwaway_db("taxup", encoding) as (async_url, sync_url):
        upgrade_to(sync_url, start)
        old, pt = _seed(sync_url)
        balance = _trial_balance(sync_url, old)
        owed = {e: s["total"] for e, s in _states(sync_url, old, "doc").items()}

        _migrate_to_head(async_url)
        from celerp.services.doc_balance import amount_due

        assert _trial_balance(sync_url, old) == balance
        assert {e: amount_due(s) for e, s in _states(sync_url, old, "doc").items()} == owed
        pt_list = _settings(sync_url, pt)["taxes"]
        assert [(c["name"], c["versions"][-1]["rate"]) for c in pt_list] == [(c["name"], c["rate"]) for c in _PT]
        settings = _settings(sync_url, old)
        for c in [*pt_list, *settings["taxes"], *settings["purchasing_taxes"]]:
            assert c["id"] and c["versions"] and "category" in c, c
        vat = next(c for c in settings["taxes"] if c["name"] == "VAT 7%")
        items = _states(sync_url, old, "item")
        assert items["item:known"]["tax_codes"] == [vat["id"]]
        assert items["item:gone"]["tax_codes"] == ["Gone 9%"]
