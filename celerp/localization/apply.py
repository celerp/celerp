# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Update localization: apply the company's country pack to its tax list.

``preview`` lists what applying the pack changes: codes the list lacks (added), dated versions a
code lacks (added version), a category the list has not set (retag) and codes whose rates were
changed by hand (kept, untouched). Its fingerprint binds a confirm to the list and pack it was
worked out from. ``apply`` writes it as one ``sys.localization.applied`` event holding each field
before and after, once per idempotency key; ``undo`` puts back, code by code, what an operation
changed and keeps (and lists) every code edited since.
"""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from celerp.accounting_roles import catalog_refusal
from celerp.events.engine import emit_event, find_event_by_idempotency, refuse_other_request, request_fingerprint
from celerp.localization import stored_rows
from celerp.localization.taxcodes import TaxVersion
from celerp.models.ledger import LedgerEntry
from celerp.tax_regimes import get_regime, tax_country, tax_name_key, with_taxes

EVENT = "sys.localization.applied"
ENTITY = "localization_operation"
# The settings an operation writes; undo compares and swaps each.
FIELDS = ("taxes", "localization")


def _pack(settings: dict) -> tuple[str, dict]:
    """The company's country and its loaded pack; 409 naming the next step when there is none."""
    code = tax_country(settings.get("country"))
    if not code:
        raise HTTPException(status_code=409, detail=catalog_refusal("localization.no_pack"))
    return code, get_regime(code)


def _versions(row: dict) -> list[dict]:
    """A list row's or pack code's rate versions, in one canonical form; a row stored with one
    undated ``rate`` is one open version (a withheld tax stores its rate negative)."""
    raw = row["versions"] if "versions" in row else [{"rate": abs(float(row.get("rate") or 0))}]
    return [TaxVersion.model_validate(v).model_dump(mode="json") for v in raw]


def _diff(base: list[dict], codes: list[dict]) -> tuple[dict, list[dict]]:
    """What applying ``codes`` to ``base`` changes, and the list it gives."""
    by_name = {tax_name_key(row.get("name")): i for i, row in enumerate(base)}
    result = [dict(row) for row in base]
    diff: dict[str, list] = {"added": [], "added_versions": [], "retagged": [], "kept": []}
    for code in codes:
        i = by_name.get(tax_name_key(code["name"]))
        if i is None:
            diff["added"].append(code)
            result.extend(stored_rows([code]))
            continue
        row, have, want = result[i], _versions(result[i]), _versions(code)
        extra = [v for v in want if v not in have]
        if any(v not in want for v in have) or (extra and "versions" not in row):
            diff["kept"].append({"name": row["name"]})
            continue
        if extra:
            row["versions"] = sorted(have + extra, key=lambda v: v["valid_from"] or "")
            diff["added_versions"].append({"name": row["name"], "versions": extra})
        if row.get("category") is None and code.get("category") is not None:
            row["category"] = code["category"]
            diff["retagged"].append({"name": row["name"], "category": code["category"]})
    return diff, result


def _binding(settings: dict, code: str, pack: dict) -> str:
    return request_fingerprint({"taxes": settings.get("taxes"), "localization": settings.get("localization"),
                                "country": code, "sha256": pack["sha256"]})


def preview(settings: dict) -> dict:
    """``{"diff", "fingerprint"}`` for applying the company's pack. A list never saved is the
    neutral starting list, which the pack replaces rather than joins."""
    code, pack = _pack(settings)
    diff, _ = _diff(settings.get("taxes") or [], pack["codes"])
    return {"diff": diff, "fingerprint": _binding(settings, code, pack)}


async def _operation(session, company_id, operation_id: str) -> LedgerEntry | None:
    return (await session.execute(select(LedgerEntry).where(
        LedgerEntry.company_id == company_id, LedgerEntry.event_type == EVENT,
        LedgerEntry.entity_id == f"{ENTITY}:{operation_id}"))).scalars().first()


async def _record(session, company, user_id, key: str, data: dict, after: dict) -> None:
    """Emit the operation, then write ``after`` into the company's settings (same transaction)."""
    await emit_event(session, company_id=company.id, entity_id=f"{ENTITY}:{data['operation_id']}",
                     entity_type=ENTITY, event_type=EVENT, actor_id=user_id, location_id=None,
                     source="api", idempotency_key=key, metadata_={}, data=data)
    settings = dict(company.settings or {})
    for field in FIELDS:
        if after[field] is None:
            settings.pop(field, None)
        else:
            settings[field] = after[field]
    company.settings = settings


async def apply(session, company, user_id, binding: str, idempotency_key: str) -> str:
    """Apply the previewed pack (``company`` loaded under the company lock); returns the
    operation id. A key already used replays its operation when it confirmed the same preview."""
    key = f"localization:apply:{idempotency_key}"
    if (done := await find_event_by_idempotency(session, company.id, key)) is not None:
        refuse_other_request(done.data.get("binding"), binding)
        return done.data["operation_id"]
    settings = dict(company.settings or {})
    code, pack = _pack(settings)
    if binding != _binding(settings, code, pack):
        raise HTTPException(status_code=409, detail=catalog_refusal("localization.stale_preview"))
    _, taxes = _diff(settings.get("taxes") or [], pack["codes"])
    operation_id = uuid.uuid4().hex
    before = {field: settings.get(field) for field in FIELDS}
    after = {"taxes": with_taxes(settings, taxes)["taxes"],
             "localization": {"country": code, "version": pack["version"], "digest": pack["sha256"],
                              "applied_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                              "operation_id": operation_id}}
    await _record(session, company, user_id, key, {
        "operation_id": operation_id, "undoes": None, "binding": request_fingerprint(binding),
        "before": before, "after": after, "kept": []}, after)
    return operation_id


def _rows(value) -> dict[str, dict]:
    return {tax_name_key(row.get("name")): row for row in value or []}


async def undo(session, company, user_id, operation_id: str) -> list[dict]:
    """Put back what operation ``operation_id`` changed wherever it still stands as the operation
    left it; returns the codes kept because they were edited since (``[{"name"}]``)."""
    target = await _operation(session, company.id, operation_id)
    if target is None:
        raise HTTPException(status_code=404, detail=catalog_refusal("localization.no_operation"))
    key = f"localization:undo:{operation_id}"
    if (done := await find_event_by_idempotency(session, company.id, key)) is not None:
        return done.data["kept"]
    settings = dict(company.settings or {})
    was, left = target.data["before"], target.data["after"]
    before, kept = {field: settings.get(field) for field in FIELDS}, []
    old, new = _rows(was["taxes"]), _rows(left["taxes"])
    taxes = []
    for row in settings.get("taxes") or []:
        name = tax_name_key(row.get("name"))
        if old.get(name) == new.get(name) or row != new.get(name):
            if old.get(name) != new.get(name):
                kept.append({"name": row.get("name")})
            taxes.append(row)
        elif name in old:
            taxes.append(old[name])
    # A list the operation started from unset goes back to unset once nothing is left in it.
    after = {"taxes": None if was["taxes"] is None and not taxes else taxes,
             "localization": was["localization"] if settings.get("localization") == left["localization"]
             else settings.get("localization")}
    await _record(session, company, user_id, key, {
        "operation_id": uuid.uuid4().hex, "undoes": operation_id, "binding": None,
        "before": before, "after": after, "kept": kept}, after)
    return kept
