# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company copy keeps its posting accounts and where every balance was booked.

After several changes of posting account and with stock booked into more than one
inventory account, a backup restored as a new company carries the same posting
accounts and the accounts each earlier one served, the role every journal line was
posted for, and the inventory account of every lot. The copy then settles and sells
exactly as the original would, on its own books.
"""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from company_backup_support import company, download, owner, restore, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401
from test_restored_connector_safety import _local

_POSTING_KEYS = ("posting_roles_schema", "posting_roles", "posting_role_scopes", "posting_legacy_lot_account")


async def _post(client, tok: str, path: str, body: dict | None = None, status: int = 200) -> dict:
    r = await client.post(path, headers=auth(tok), json=body)
    assert r.status_code == status, r.text
    return r.json()


async def _remap(client, tok: str, role: str, code: str, parent: str) -> None:
    await _post(client, tok, "/accounting/accounts", {"code": code, "name": f"Account {code}",
                                                      "account_type": "asset", "parent_code": parent})
    r = await client.put(f"/accounting/posting-accounts/{role}", headers=auth(tok), json={"code": code})
    assert r.status_code == 200, r.text


async def _invoice(client, tok: str, total: float, lots: tuple[str, ...] = ()) -> str:
    lines = [{"entity_id": lot, "name": "Lot", "quantity": 1, "unit_price": total, "sell_by": "piece"}
             for lot in lots] or [{"name": "Service", "quantity": 1, "unit_price": total}]
    doc = (await _post(client, tok, "/docs", {"doc_type": "invoice", "line_items": lines,
                                              "total": total * max(len(lots), 1)}))["id"]
    await _post(client, tok, f"/docs/{doc}/finalize")
    return doc


async def _lot(client, tok: str, sku: str, cost: float) -> str:
    return (await _post(client, tok, "/items", {"sku": sku, "name": "Lot", "quantity": 1, "sell_by": "piece",
                                                "status": "available", "cost_total": cost}))["id"]


async def _rows(engine, cid, sql: str, **params) -> list:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), {"c": uuid.UUID(str(cid)), **params})).all()


async def _settings(engine, cid) -> dict:
    (row,) = await _rows(engine, cid, "SELECT settings FROM companies WHERE id = :c")
    settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])
    return {k: settings.get(k) for k in _POSTING_KEYS}


async def _journal(engine, cid) -> dict[str, list]:
    """Every automatic entry's lines: account, role and amounts."""
    rows = await _rows(engine, cid, "SELECT entity_id, state FROM projections WHERE company_id = :c "
                                    "AND entity_type = 'journal_entry' AND entity_id LIKE 'je:auto:%'")
    return {eid: sorted((e["account"], tuple(e.get("account_roles") or ()), e.get("debit") or 0,
                         e.get("credit") or 0) for e in state["entries"]) for eid, state in rows}


async def _lots(engine, cid) -> dict[str, str | None]:
    rows = await _rows(engine, cid, "SELECT state FROM projections WHERE company_id = :c AND entity_type = 'item'")
    return {state["sku"]: state.get("inventory_account_code") for (state,) in rows if state.get("sku")}


async def _accounts(engine, cid) -> dict[str, tuple]:
    rows = await _rows(engine, cid, "SELECT code, account_type, is_active FROM accounts WHERE company_id = :c")
    return {code: (kind, active) for code, kind, active in rows}


async def _entry(engine, cid, entity_id: str) -> dict[str, tuple]:
    (row,) = await _rows(engine, cid, "SELECT state FROM projections WHERE company_id = :c AND entity_id = :e",
                         e=entity_id)
    return {e["account"]: (e.get("debit") or 0, e.get("credit") or 0) for e in row[0]["entries"]}


async def _net(engine, cid, code: str) -> float:
    rows = await _rows(engine, cid, "SELECT state FROM projections WHERE company_id = :c "
                                    "AND entity_type = 'journal_entry'")
    return round(sum((e.get("debit") or 0) - (e.get("credit") or 0) for (state,) in rows
                     if state.get("status") == "posted" for e in state["entries"] if e["account"] == code), 2)


@pytest.mark.asyncio
async def test_a_restored_copy_keeps_posting_accounts_and_every_origin(real_engine, real_client, tmp_path,
                                                                       monkeypatch):
    from test_helpers import provision_company_books

    _local(monkeypatch, tmp_path)
    user = await owner(real_engine)
    source = await company(real_engine, user, "Origin Trading", "origin-marker", settings={"currency": "USD"})
    async with maker(real_engine)() as s:
        await provision_company_books(s, source)
        await s.commit()
    tok = await token(real_engine, user, source)

    old_invoice = await _invoice(real_client, tok, 100.0)
    old_lot = await _lot(real_client, tok, "LOT-OLD", 30.0)
    older_lot = await _lot(real_client, tok, "LOT-OLDER", 25.0)
    async with maker(real_engine)() as s:  # booked before lots recorded their account
        await s.execute(text("UPDATE projections SET state = (state::jsonb - 'inventory_account_code')::json "
                             "WHERE company_id = :c AND entity_id = :e"), {"c": source, "e": older_lot})
        await s.commit()
    await _remap(real_client, tok, "receivable", "1121", "1100")
    await _remap(real_client, tok, "inventory_purchased", "1131", "1130")
    new_invoice = await _invoice(real_client, tok, 40.0)
    await _lot(real_client, tok, "LOT-NEW", 20.0)
    await _remap(real_client, tok, "receivable", "1122", "1100")

    before = (await _settings(real_engine, source), await _journal(real_engine, source),
              await _lots(real_engine, source), await _accounts(real_engine, source))
    assert before[0]["posting_role_scopes"]["receivable"] == ["1120", "1121", "1122"]
    assert before[2] == {"LOT-OLD": "1130-P", "LOT-OLDER": None, "LOT-NEW": "1131"}

    r = await restore(real_client, tok, await download(real_client, tok), mode="new_company")
    assert r.status_code == 201, r.text
    copy, copy_tok = r.json()["company_id"], r.json()["access_token"]
    assert str(copy) != str(source)
    after = (await _settings(real_engine, copy), await _journal(real_engine, copy),
             await _lots(real_engine, copy), await _accounts(real_engine, copy))
    assert after == before

    # The copy settles and sells on the accounts each balance was booked into.
    for doc, total in ((old_invoice, 100.0), (new_invoice, 40.0)):
        r = await real_client.post(f"/docs/{doc}/payment", headers=auth(copy_tok), json={
            "amount": total, "payment_date": "2026-03-02", "bank_account": "1111"})
        assert r.status_code == 200, r.text
    assert await _entry(real_engine, copy, f"je:auto:{old_invoice}:pay:0") == {"1111": (100.0, 0), "1120": (0, 100.0)}
    assert await _entry(real_engine, copy, f"je:auto:{new_invoice}:pay:0") == {"1111": (40.0, 0), "1121": (0, 40.0)}
    lot_ids = {sku: eid for eid, sku in await _rows(
        real_engine, copy, "SELECT entity_id, state ->> 'sku' FROM projections WHERE company_id = :c "
                           "AND entity_type = 'item'")}
    sale = await _invoice(real_client, copy_tok, 50.0, (lot_ids["LOT-OLD"], lot_ids["LOT-OLDER"], lot_ids["LOT-NEW"]))
    sold = await _entry(real_engine, copy, f"je:auto:{sale}:fin")
    assert {code: amounts[1] for code, amounts in sold.items() if code.startswith("113")} == {
        "1130-P": 55.0, "1131": 20.0}
    assert sold["1122"] == (150.0, 0)
    await _lot(real_client, copy_tok, "LOT-COPY", 5.0)
    assert (await _lots(real_engine, copy))["LOT-COPY"] == "1131"

    # The original's books did not move.
    assert (await _net(real_engine, source, "1120"), await _net(real_engine, source, "1121")) == (100.0, 40.0)
    assert await _settings(real_engine, source) == before[0]


@pytest.mark.asyncio
async def test_a_restored_copy_keeps_a_merge_across_inventory_accounts(real_engine, real_client, tmp_path,
                                                                       monkeypatch):
    from test_helpers import provision_company_books

    _local(monkeypatch, tmp_path)
    user = await owner(real_engine)
    source = await company(real_engine, user, "Merge Trading", "merge-marker", settings={"currency": "USD"})
    async with maker(real_engine)() as s:
        await provision_company_books(s, source)
        await s.commit()
    tok = await token(real_engine, user, source)

    a = await _lot(real_client, tok, "LOT-A", 600.0)
    await _remap(real_client, tok, "inventory_purchased", "1131", "1130")
    b = await _lot(real_client, tok, "LOT-B", 400.0)
    merged = (await _post(real_client, tok, "/items/merge", {"source_entity_ids": [a, b], "target_sku_from": a}))["id"]
    reclass = f"je:auto:{merged}:merge-reclass"
    assert await _entry(real_engine, source, reclass) == {"1130-P": (400.0, 0), "1131": (0, 400.0)}

    before = (await _journal(real_engine, source), await _lots(real_engine, source))
    r = await restore(real_client, tok, await download(real_client, tok), mode="new_company")
    assert r.status_code == 201, r.text
    copy, copy_tok = r.json()["company_id"], r.json()["access_token"]
    assert (await _journal(real_engine, copy), await _lots(real_engine, copy)) == before
    assert await _entry(real_engine, copy, reclass) == {"1130-P": (400.0, 0), "1131": (0, 400.0)}
    (merged_state,) = await _rows(real_engine, copy, "SELECT state FROM projections WHERE company_id = :c "
                                                     "AND entity_id = :e", e=merged)
    assert (merged_state[0]["inventory_account_code"], merged_state[0]["cost_total"]) == ("1130-P", 1000.0)

    # The copy can undo the merge exactly as the original could.
    await _post(real_client, copy_tok, f"/items/{merged}/undo-merge")
    (row,) = await _rows(real_engine, copy, "SELECT state ->> 'status' FROM projections WHERE company_id = :c "
                                            "AND entity_id = :e", e=reclass)
    assert row[0] == "void"
    assert (await _lots(real_engine, copy))["LOT-B"] == "1131"
    assert await _entry(real_engine, source, reclass) == {"1130-P": (400.0, 0), "1131": (0, 400.0)}
