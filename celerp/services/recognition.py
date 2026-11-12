# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Deferred revenue at finalize: which sale lines defer, over what period, and the schedule
each one makes.

A line defers when it is not an asset line, its item's category is not a gift category
(``gift_categories``, a company setting of category keys) and its item, or the agreement the
document bills, recognizes over the service period (``RECOGNIZE_FIELD``). Its share of the
sale credits the deferred revenue account instead of its revenue account, and a schedule
``rev:{doc}:{line_id}`` (a ``rev_schedule`` projection) records what is to be recognized:
the source line, the revenue account it would have credited, the deferred account, the
fund, the currencies and rate, the source entry and segments of {start, end, amount}. The
monthly runs that recognize it are planned from the schedule and the ledger
(celerp_accounting.recognition); nothing recognized is stored on the schedule.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import RECOGNIZE_FIELD, catalog_refusal
from celerp.models.projections import Projection
from celerp.services.document_lines import line_item_id

GIFT_CATEGORIES = "gift_categories"
SCHEDULE = "rev_schedule"
BASIS = "daily"


def schedule_id(doc_id: str, line_id: str) -> str:
    return f"rev:{doc_id}:{line_id}"


def gift_categories(settings: Mapping) -> frozenset[str]:
    return frozenset(settings.get(GIFT_CATEGORIES) or ())


def deferred_line(settings: Mapping, line: Mapping, item: Mapping | None, agreement: Mapping | None) -> bool:
    """Whether ``line`` defers its revenue: not an asset line, its item outside the gift
    categories, and the item's or the source agreement's flag on."""
    if line.get("asset_id"):
        return False
    item = item or {}
    if item.get("category") and str(item["category"]) in gift_categories(settings):
        return False
    return bool(item.get(RECOGNIZE_FIELD) or (agreement or {}).get(RECOGNIZE_FIELD))


def _iso_day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def service_period(doc: Mapping, line: Mapping) -> tuple[date, date] | None:
    """The line's own service period, else the document's; None when there is none or it
    ends before it starts."""
    start, end = ((line.get("service_start"), line.get("service_end"))
                  if line.get("service_start") or line.get("service_end")
                  else (doc.get("service_start"), doc.get("service_end")))
    start, end = _iso_day(start), _iso_day(end)
    return (start, end) if start and end and end >= start else None


async def _states(session, company_id, entity_type: str, ids: set) -> dict[str, dict]:
    ids = {str(i) for i in ids if i}
    if not ids:
        return {}
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == company_id, Projection.entity_type == entity_type,
        Projection.entity_id.in_(sorted(ids))))).scalars().all()
    return {row.entity_id: row.state or {} for row in rows}


async def deferred_lines(session, company_id, doc: Mapping, settings: Mapping) -> dict[str, tuple[date, date]]:
    """The service period of each line of a sale that defers (deferred_line), keyed by the
    line's index as a string. Refuses (422, naming the line) a deferred line with no period
    or one ending before it starts."""
    lines = [li if isinstance(li, dict) else {} for li in doc.get("line_items") or []]
    items = await _states(session, company_id, "item", {line_item_id(li) for li in lines})
    agreements = await _states(session, company_id, "doc",
                               {li.get("source_agreement") or doc.get("source_agreement") for li in lines})
    periods: dict[str, tuple[date, date]] = {}
    for index, li in enumerate(lines):
        agreement = agreements.get(str(li.get("source_agreement") or doc.get("source_agreement") or ""))
        if not deferred_line(settings, li, items.get(str(line_item_id(li))), agreement):
            continue
        period = service_period(doc, li)
        if period is None:
            raise HTTPException(status_code=422, detail=catalog_refusal(
                "recognition.err_service_period", line=li.get("name") or str(index + 1)))
        periods[str(index)] = period
    return periods


def schedule_records(doc_id: str, doc: Mapping, *, periods: Mapping[str, tuple[date, date]],
                     allocations: Mapping[str, dict], revenue_accounts: Mapping[str, str], deferred: str,
                     funds: Mapping[int, str | None], books_currency: str, rate, source_je: str) -> list[dict]:
    """The schedule each deferred line makes, as the ``rev_schedule.created`` data: its line's
    share in the books' currency (``allocations``) recognized over its period in one segment."""
    lines = doc.get("line_items") or []
    records = []
    for index, (start, end) in periods.items():
        line = lines[int(index)]
        records.append({
            "doc_id": doc_id, "line_id": line.get("line_id"), "line": int(index) + 1,
            "account": revenue_accounts[index], "deferred_account": deferred,
            "fund_id": funds.get(int(index) + 1), "currency": books_currency,
            "doc_currency": doc.get("currency") or books_currency, "rate": float(rate),
            "basis": BASIS, "source_je": source_je,
            "segments": [{"start": start.isoformat(), "end": end.isoformat(),
                          "amount": allocations[index]["base"]}],
            "amendments": [],
        })
    return records


def amendment(kind: str, effective: str, reason: str | None, user_id) -> dict:
    return {"kind": kind, "effective": effective, "reason": reason, "by": str(user_id) if user_id else None,
            "at": datetime.now(timezone.utc).isoformat()}
