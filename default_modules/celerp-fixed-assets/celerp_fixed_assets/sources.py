# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Source lines and their claims.

A source line is a posted bill line on a class asset account (``<doc_id>:<line_id>``) or
a posted manual journal line debiting a class asset account or crediting a class
accumulated depreciation account (``<je_id>:<index>``). Lines dated on or before the
cutover belong to the opening register and are never offered. A line is claimed once:
the claim is the event ``fa:src:<source>:claim:<generation>``, and a release adds one to
the generation, so an old form can never reclaim a line someone claimed and released
meanwhile.
"""
from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote_plus

from celerp.models.projections import Projection
from celerp.services.money import books_currency
from celerp_fixed_assets import setup
from celerp_fixed_assets.projections import _dec
from sqlalchemy import select

BILL_TYPES = ("bill",)


def claim_id(source: str) -> str:
    return f"fa:src:{source}"


def _quantity(value):
    q = _dec(value) or Decimal(1)
    return int(q) if q == q.to_integral_value() else str(q)


def _day(ts) -> str:
    return str(ts or "")[:10]


async def _posted_journals(session, company_id, ids: list[str] | None = None, prefix: str | None = None) -> dict:
    query = select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "journal_entry",
        Projection.state["status"].as_string() == "posted")
    if ids is not None:
        query = query.where(Projection.entity_id.in_(ids))
    if prefix is not None:
        query = query.where(Projection.entity_id.startswith(prefix, autoescape=True))
    return {eid: state for eid, state in (await session.execute(query)).all()}


async def _bill_lines(session, company_id, company_settings: dict, asset_accounts: set[str]) -> list[dict]:
    from celerp.services.account_roles import line_has_role
    from celerp.services.auto_je import bill_line_base

    docs = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(BILL_TYPES)))).all()
    journals = await _posted_journals(session, company_id, ids=[f"je:auto:{d}:bill" for d, _ in docs])
    base = books_currency(company_settings)
    out = []
    for doc_id, doc in docs:
        je = journals.get(f"je:auto:{doc_id}:bill")
        if je is None:
            continue
        amounts = None
        # A line received as an asset posted to the account then serving fixed assets.
        fixed = next((e.get("account") for e in je.get("entries") or []
                      if line_has_role(company_settings, e, "fixed_assets")), None)
        for idx, li in enumerate(doc.get("line_items") or []):
            account = li.get("account_code") or (
                fixed if (li.get("receive_as") or "").strip().lower() == "asset" else None)
            if account not in asset_accounts:
                continue
            amounts = amounts if amounts is not None else bill_line_base(doc, base)
            out.append({
                "source": f"{doc_id}:{li.get('line_id') or idx}", "kind": "acquisition",
                "description": li.get("name") or li.get("description") or "", "account": account,
                "amount": str(amounts.get(idx, Decimal(0))), "quantity": _quantity(li.get("quantity")),
                "date": _day(je.get("ts")), "journal_id": f"je:auto:{doc_id}:bill", "line_index": idx,
                "document": doc_id, "document_ref": doc.get("ref_id"), "href": f"/docs/{doc_id}"})
    return out


async def _manual_lines(session, company_id, asset_accounts: set[str], accum_accounts: set[str]) -> list[dict]:
    out = []
    for je_id, je in (await _posted_journals(session, company_id, prefix="je:manual:")).items():
        for idx, entry in enumerate(je.get("entries") or []):
            debit, credit, account = _dec(entry.get("debit")), _dec(entry.get("credit")), entry.get("account")
            if debit > 0 and account in asset_accounts:
                kind, amount = "acquisition", debit
            elif credit > 0 and account in accum_accounts:
                kind, amount = "depreciation", credit
            elif credit > 0 and account in asset_accounts:
                kind, amount = "reduction", credit
            else:
                continue
            out.append({
                "source": f"{je_id}:{idx}", "kind": kind, "description": je.get("memo") or "",
                "account": account, "amount": str(amount), "quantity": 1, "date": _day(je.get("ts")),
                "journal_id": je_id, "line_index": idx, "document": je_id, "document_ref": None,
                "href": f"/accounting?q={quote_plus(je_id)}"})
    return out


async def lines(session, company_id) -> list[dict]:
    """Every source line, claimed or not, before or after the cutover, each with its claim."""
    company_settings = await setup.company_settings(session, company_id)
    fa = setup.settings_of(company_settings)
    classes = await setup.classes(session, company_id, include_archived=True)
    asset_accounts = {c["asset_account"] for c in classes if c.get("asset_account")}
    accum_accounts = {c["accumulated_depreciation_account"] for c in classes
                      if c.get("accumulated_depreciation_account")}
    found = [*await _bill_lines(session, company_id, company_settings, asset_accounts),
             *await _manual_lines(session, company_id, asset_accounts, accum_accounts)]
    claims = await _claims(session, company_id, [f["source"] for f in found])
    labels = await setup.account_labels(session, company_id, [f["account"] for f in found])
    threshold = _dec(fa.get("threshold")) if fa.get("threshold") else None
    cut = setup.cutover(fa)
    for f in found:
        claim = claims.get(claim_id(f["source"])) or {}
        per_unit = _dec(f["amount"]) / _dec(f["quantity"])
        f.update(generation=int(claim.get("generation") or 0), account_label=labels[f["account"]],
                 claim=claim if claim.get("status") == "claimed" else None,
                 before_cutover=bool(cut) and f["date"] <= cut,
                 below_threshold=threshold is not None and per_unit < threshold)
    found.sort(key=lambda f: (f["date"], f["source"]), reverse=True)
    return found


async def _claims(session, company_id, sources: list[str]) -> dict[str, dict]:
    if not sources:
        return {}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "fa_src",
        Projection.entity_id.in_([claim_id(s) for s in sources])))).all()
    return dict(rows)


async def intake(session, company_id) -> list[dict]:
    """The lines waiting to be registered: unclaimed and after the cutover, newest first. A
    line reducing an asset account is not registered; it is linked to its asset by Correct."""
    return [{k: v for k, v in f.items() if k not in ("claim", "before_cutover")}
            for f in await lines(session, company_id)
            if not f["claim"] and not f["before_cutover"] and f["kind"] != "reduction"]


async def line(session, company_id, source: str) -> dict:
    found = next((f for f in await lines(session, company_id) if f["source"] == source), None)
    if found is None:
        raise setup.unprocessable("That source line is not a posted asset line.")
    return found

