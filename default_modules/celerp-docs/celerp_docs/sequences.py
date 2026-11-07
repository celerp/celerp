# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import Refused, catalog_refusal
from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.business_time import business_today
from celerp.services.company_lock import lock_company

_PREFIX_BY_DOC_TYPE = {
    "invoice": "INV",
    "proforma": "PF",
    "purchase_order": "PO",
    "quotation": "QUO",
    "shipping_doc": "SHIP",
    "credit_note": "CN",
    "debit_note": "SCN",
    "bill": "BILL",
    "memo": "MEMO",
    "list": "LST",
    "transfer": "TRF",
    "consignment_in": "CI",
    "subscription_invoice": "SUBI",
    "subscription_po": "SUBP",
    "production_order": "PRD",
    "audit": "AUD",
    "writeoff": "WO",
}


# Every list type has its own numbering counter.
_LIST_PREFIX_KEYS = {"quotation", "transfer", "audit", "writeoff", "shipping_doc"}


def list_sequence_key(list_type: str | None, settings: dict | None = None) -> str:
    """The counter a List of ``list_type`` draws. A company that still has the shared "list"
    counter (LST, which numbered every List before each type had its own) keeps it for
    quotation and untyped Lists, and for transfers until the transfer counter is saved. The
    quotation counter is also the quotation documents' counter, so it never ends the fallback."""
    key = list_type if list_type in _LIST_PREFIX_KEYS else "quotation"
    sequences = (settings or {}).get("sequences") or {}
    if sequences.get("list") and (key == "quotation" or (key == "transfer" and not sequences.get(key))):
        return "list"
    return key


# Default pattern for all new companies
DEFAULT_PATTERN = "{PREFIX}-{YY}{MM}-{####}"


def _expand_pattern(pattern: str, prefix: str, day: date, seq_num: int) -> str:
    """Expand a numbering pattern into a concrete document reference."""
    # Count # chars to determine padding width
    match = re.search(r"\{(#+)\}", pattern)
    pad = len(match.group(1)) if match else 2

    result = pattern
    result = result.replace("{PREFIX}", prefix)
    result = result.replace("{YYYY}", str(day.year))
    result = result.replace("{YY}", f"{day.year % 100:02d}")
    result = result.replace("{MM}", f"{day.month:02d}")
    result = result.replace("{DD}", f"{day.day:02d}")
    # Replace the {##...} token with zero-padded number
    result = re.sub(r"\{#+\}", f"{seq_num:0{pad}d}", result)
    return result


def _period(pattern: str, day: date) -> tuple:
    """The numbering period ``day`` falls in, as far as the pattern can tell periods apart.

    No year token: one period forever. A month needs the year and a day needs the month,
    so {DD} without {MM} counts through the year and {MM} without a year never resets."""
    if "{YYYY}" not in pattern and "{YY}" not in pattern:
        return ()
    if "{MM}" not in pattern:
        return (day.year,)
    if "{DD}" not in pattern:
        return (day.year, day.month)
    return (day.year, day.month, day.day)


def _stored_day(seq: dict) -> date | None:
    """The day the counter last numbered a document, or None when it never has."""
    try:
        year = int(seq.get("year") or 0)
        return date(year, int(seq.get("month") or 1), int(seq.get("day") or 1)) if year else None
    except (TypeError, ValueError):
        return None


def _numbering_day(settings: dict, seq: dict) -> date:
    """The company's business day, never earlier than the day the counter last used, so a
    timezone change cannot move numbering back into a finished period."""
    today = business_today(settings.get("timezone"))
    stored = _stored_day(seq)
    return max(today, stored) if stored else today


def _next_number(seq: dict, pattern: str, day: date) -> int:
    """The number the next document on ``day`` takes: 1 once ``day`` is in a later period
    than the last numbered document, otherwise the saved next number."""
    stored = _stored_day(seq)
    if stored and _period(pattern, day) > _period(pattern, stored):
        return 1
    return int(seq.get("next") or 1)


def validate_pattern(pattern: str) -> str | None:
    """Return error message if pattern is invalid, None if OK."""
    if not re.search(r"\{#+\}", pattern):
        return "Pattern must contain a sequence token like {##} or {####}"
    # Check for unknown tokens
    cleaned = re.sub(r"\{(PREFIX|YYYY|YY|MM|DD|#+)\}", "", pattern)
    unknown = re.findall(r"\{[^}]+\}", cleaned)
    if unknown:
        return f"Unknown tokens: {', '.join(unknown)}"
    return None


def require_doc_type(doc_type: str) -> str:
    """Return ``doc_type`` when it is a document type this app numbers, else refuse it by name."""
    if doc_type not in _PREFIX_BY_DOC_TYPE:
        raise ValueError(
            f"{doc_type!r} is not a document type. Use one of: {', '.join(sorted(_PREFIX_BY_DOC_TYPE))}.")
    return doc_type


def next_doc_ref(company: Company, doc_type: str) -> str:
    """Generate the next document reference and increment the sequence counter.

    ``company`` must come from locked_company(), taken before any document row lock;
    a counter change on any other company object is refused at flush."""
    require_doc_type(doc_type)

    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    seq = dict(sequences.get(doc_type) or {})

    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)

    day = _numbering_day(settings, seq)
    next_num = _next_number(seq, pattern, day)
    ref = _expand_pattern(pattern, prefix, day, next_num)

    # Update sequence state
    seq["next"] = next_num + 1
    seq["year"] = day.year
    seq["month"] = day.month
    seq["day"] = day.day
    seq["prefix"] = prefix
    seq["pattern"] = pattern
    sequences[doc_type] = seq
    settings["sequences"] = sequences
    company.settings = settings

    return ref


def draft_counter(doc_type: str) -> str:
    """The counter a new draft of ``doc_type`` is numbered from. A draft invoice is numbered
    from the proforma counter; finalize gives it its invoice number, so no invoice number goes
    unused."""
    return "proforma" if doc_type == "invoice" else doc_type


async def number_holder(session: AsyncSession, company_id, number: str, *,
                        exclude_entity_id: str | None = None,
                        doc_type: str | None = None) -> tuple[str, str] | None:
    """(kind, number shown) for the Document or List ("doc" or "list") that shows ``number``, or
    for a new record (no ``exclude_entity_id``) is stored under it, ignoring case as imports do;
    None when the number is free. Documents and Lists share one number space, so every number
    is checked against this one rule. ``doc_type`` narrows the Documents that count to that
    type, for imported books, where another system's bill and invoice may carry the same
    number; every List still counts."""
    wanted = number.strip().lower()
    shown = Projection.state["ref_id"].as_string()
    doc_number = Projection.state["doc_number"].as_string()
    matches = [func.lower(shown) == wanted, func.lower(doc_number) == wanted]
    if not exclude_entity_id:
        # A new record is stored under its number, so a key another record is stored under counts too.
        matches.append(func.lower(Projection.entity_id).in_((f"doc:{wanted}", f"list:{wanted}")))
    query = select(Projection.entity_type, Projection.entity_id, shown, doc_number).where(
        Projection.company_id == company_id,
        Projection.entity_type.in_(("doc", "list")),
        or_(*matches),
    )
    if exclude_entity_id:
        # The record taking the number keeps its own storage key, so only shown numbers count.
        query = query.where(Projection.entity_id != exclude_entity_id)
    if doc_type:
        query = query.where(or_(Projection.entity_type == "list",
                                Projection.state["doc_type"].as_string() == doc_type))
    row = (await session.execute(query.limit(1))).first()
    if row is None:
        return None
    kind, entity_id, ref_id, number_shown = row
    return kind, ref_id or number_shown or entity_id.split(":", 1)[1]


async def require_number_free(session: AsyncSession, company_id, number: str, *,
                              exclude_entity_id: str | None = None, doc_type: str | None = None) -> None:
    """Refuse ``number`` while another Document or List shows it (number_holder), naming which
    kind holds it and the number as that record shows it. The company lock serializes the
    check with every other numbering write."""
    await lock_company(session, company_id)
    holder = await number_holder(session, company_id, number, exclude_entity_id=exclude_entity_id,
                                 doc_type=doc_type)
    if holder is not None:
        kind, shown = holder
        key = "documents.err_doc_number_taken" if kind == "doc" else "documents.err_list_number_taken"
        raise HTTPException(status_code=409, detail=catalog_refusal(key, number=shown))


async def claim_number(session: AsyncSession, company: Company, counter: str) -> str:
    """The next number of ``counter`` that no Document or List holds. A number a user, an
    import or another counter already gave is skipped, never reused, and nothing is
    renumbered. ``company`` comes from locked_company(), as for next_doc_ref()."""
    while True:
        number = next_doc_ref(company, counter)
        if await number_holder(session, company.id, number) is None:
            return number


def peek_draft_ref(company: Company, doc_type: str) -> str:
    """The reference the next new draft of ``doc_type`` would take, without taking it."""
    settings = dict(company.settings or {})
    counter = draft_counter(doc_type)
    return _sequence_view(settings, counter, dict((settings.get("sequences") or {}).get(counter) or {}))["preview"]


def _row_counter(doc_type: str, settings: dict) -> str | None:
    """The counter the Numbering row for ``doc_type`` shows, or None when nothing draws it:
    the transfer row shows the shared counter while transfers draw it, and the shared
    counter has its own row only while the company has it."""
    if doc_type == "list":
        return "list" if (settings.get("sequences") or {}).get("list") else None
    return list_sequence_key(doc_type, settings) if doc_type == "transfer" else doc_type


def get_all_sequences(company: Company) -> list[dict]:
    """The numbering config of every counter something draws, one row per doc type."""
    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    return [
        _sequence_view(settings, doc_type, dict(sequences.get(counter) or {}))
        for doc_type in _PREFIX_BY_DOC_TYPE
        if (counter := _row_counter(doc_type, settings))
    ]


def _sequence_view(settings: dict, doc_type: str, seq: dict) -> dict:
    """A counter as the Numbering tab shows it; the preview is the reference the next
    document of this type gets."""
    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[doc_type])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
    day = _numbering_day(settings, seq)
    return {
        "doc_type": doc_type,
        "prefix": prefix,
        "pattern": pattern,
        "next": int(seq.get("next") or 1),
        "preview": _expand_pattern(pattern, prefix, day, _next_number(seq, pattern, day)),
    }


def _namespace(counter: str, seq: dict) -> str:
    """The numbers a counter can give, as its pattern with its prefix filled in."""
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
    return pattern.replace("{PREFIX}", str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[counter]))


def update_sequence(company: Company, doc_type: str, prefix: str | None = None,
                    pattern: str | None = None, next_num: int | None = None) -> dict:
    """Update numbering config for a single doc type. Returns the updated sequence.

    ``company`` must come from locked_company(), as for next_doc_ref()."""
    require_doc_type(doc_type)

    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    # Saving the transfer row while transfers draw the shared counter gives them their own.
    inherited = sequences.get("list") if doc_type != "list" == _row_counter(doc_type, settings) else None
    seq = dict(sequences.get(doc_type) or inherited or {})
    if inherited is not None:
        # A split counter must not mint the same references as the shared one.
        seq["prefix"] = _PREFIX_BY_DOC_TYPE[doc_type]

    if prefix is not None:
        seq["prefix"] = prefix
    if pattern is not None:
        err = validate_pattern(pattern)
        if err:
            raise ValueError(err)
        seq["pattern"] = pattern
    if next_num is not None:
        # The number belongs to today's period, so the reset for a later period cannot discard it.
        day = _numbering_day(settings, seq)
        seq.update(next=max(1, next_num), year=day.year, month=day.month, day=day.day)

    # Two counters with one format would give the same numbers, so the second is refused.
    target = _namespace(doc_type, seq)
    for kind in _PREFIX_BY_DOC_TYPE:
        counter = _row_counter(kind, settings)
        if counter and doc_type not in (kind, counter) and target == _namespace(counter, sequences.get(counter) or {}):
            raise Refused("documents.err_numbering_overlap", doc_type=kind)

    sequences[doc_type] = seq
    settings["sequences"] = sequences
    company.settings = settings

    return _sequence_view(settings, doc_type, seq)
