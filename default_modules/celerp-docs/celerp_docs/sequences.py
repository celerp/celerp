# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from datetime import date

from fastapi import HTTPException
import sqlalchemy as sa
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.accounting_roles import Refused, catalog_refusal
from celerp.models.company import Company
from celerp.models.projections import (
    KEY_NUMBER, NUMBER_KEPT, NUMBER_KEYED, NUMBER_SHOWN, Projection, canonical_number, strip_number,
)
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

# The List types the shared "list" counter (LST) numbered before each type had its own and
# still numbers in a company that has it: quotation (and untyped) and transfer Lists.
_LEGACY_SHARES = ("quotation", "transfer")


def _shares(entry: dict | None) -> tuple[str, ...]:
    """The List types the shared "list" counter numbers, read from that counter alone: the
    types it is saved with. Every save of this version stamps them (_store), so an entry
    without them was last saved before each List type had its own counter and numbers
    quotation, untyped and transfer Lists. No counter numbers none."""
    if not entry:
        return ()
    return tuple(entry["shares"]) if "shares" in entry else _LEGACY_SHARES


def list_sequence_key(list_type: str | None, settings: dict | None = None) -> str:
    """The counter a List of ``list_type`` draws: its type's own, or the shared "list" counter
    while that counter numbers the type (_shares). No other counter changes it, so writing a
    document of any type never moves List numbering."""
    key = list_type if list_type in _LIST_PREFIX_KEYS else "quotation"
    return "list" if key in _shares(((settings or {}).get("sequences") or {}).get("list")) else key


def _store(company: Company, writes: dict[str, dict]) -> None:
    """Save ``writes`` ({counter: its state}) into the company's numbering: the one writer of
    it. The shared "list" counter keeps the List types it numbers, unless the write names them."""
    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    for counter, seq in writes.items():
        if counter == "list":
            seq = {"shares": list(_shares(sequences.get("list"))), **seq}
        sequences[counter] = seq
    settings["sequences"] = sequences
    company.settings = settings


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


def require_pattern(pattern: str) -> str:
    """Return ``pattern`` when it numbers documents, else refuse it: it needs one sequence
    token and only the tokens Celerp fills in."""
    if not re.search(r"\{#+\}", pattern):
        raise Refused("documents.err_numbering_no_sequence_token", example="{####}")
    unknown = re.findall(r"\{[^}]*\}", re.sub(r"\{(PREFIX|YYYY|YY|MM|DD|#+)\}", "", pattern))
    if unknown:
        raise Refused("documents.err_numbering_unknown_tokens", tokens=", ".join(unknown))
    return pattern


# The longest number a record can have: a record is stored under its number, and a longer
# one would not fit the index of the keys records are stored under.
MAX_NUMBER_LENGTH = 100


def require_storable_number(number: str) -> str:
    """Return ``number`` when a record can be stored under it, else refuse it (422): a NUL
    character, which the database cannot hold, or a number past the limit, whether the number
    was typed or generated by a counter."""
    if "\x00" in number:
        raise HTTPException(status_code=422, detail=catalog_refusal("documents.err_number_nul"))
    if len(number) > MAX_NUMBER_LENGTH:
        raise HTTPException(status_code=422, detail=catalog_refusal(
            "documents.err_number_too_long", max=MAX_NUMBER_LENGTH, length=len(number)))
    return number


def require_doc_type(doc_type: str) -> str:
    """Return ``doc_type`` when it is a document type this app numbers, else refuse it, echoing
    the value sent as written (a label made up from it would name a type that does not exist)."""
    if doc_type not in _PREFIX_BY_DOC_TYPE:
        raise Refused("documents.err_numbering_unknown_type", value=str(doc_type),
                      types=", ".join(sorted(_PREFIX_BY_DOC_TYPE)))
    return doc_type


def _counter(settings: dict, counter: str) -> tuple[dict, str, str, date, int]:
    """(state, prefix, pattern, numbering day, next number) of ``counter``."""
    seq = dict((settings.get("sequences") or {}).get(counter) or {})
    prefix = str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[counter])
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
    day = _numbering_day(settings, seq)
    return seq, prefix, pattern, day, _next_number(seq, pattern, day)


def _first_free(settings: dict, counter: str, held: Collection[str]) -> tuple[str, dict]:
    """(the reference the next document of ``counter`` takes, the counter once it is taken):
    the first number from the counter's next one that is not in ``held`` (held_numbers). The
    preview and the number given both read this one rule, and a number too long to store is
    refused here (require_storable_number), before the counter it returns moves."""
    seq, prefix, pattern, day, number = _counter(settings, counter)
    while (ref := _expand_pattern(pattern, prefix, day, number)) in held:
        number += 1
    require_storable_number(ref)
    seq.update(next=number + 1, year=day.year, month=day.month, day=day.day, prefix=prefix, pattern=pattern)
    return ref, seq


def next_doc_ref(company: Company, doc_type: str, held: Collection[str]) -> str:
    """Take the next reference of ``doc_type``'s counter that is not in ``held`` and advance
    the counter past it.

    ``company`` must come from locked_company(), taken before any document row lock;
    a counter change on any other company object is refused at flush."""
    require_doc_type(doc_type)
    ref, seq = _first_free(dict(company.settings or {}), doc_type, held)
    _store(company, {doc_type: seq})
    return ref


def draft_counter(doc_type: str) -> str:
    """The counter a new draft of ``doc_type`` is numbered from. A draft invoice is numbered
    from the proforma counter; finalize gives it its invoice number, so no invoice number goes
    unused."""
    return "proforma" if doc_type == "invoice" else doc_type


# The fields a Document or List holds a number in. Every write of one goes through
# require_number_free and stores the number it returns.
NUMBER_FIELDS = ("ref_id", "doc_number")


# The numbers a Document or List shows, keeps and is stored under.
_NUMBERS = (NUMBER_SHOWN, NUMBER_KEPT, NUMBER_KEYED)


def _number_match(candidate: sa.ColumnElement):
    """A Document or List shows, keeps or is stored under ``candidate`` (canonical_number),
    read through the indexed expressions (ix_projections_canonical_number_*)."""
    return or_(*(number == candidate for number in _NUMBERS))


async def number_holder(session: AsyncSession, company_id, number: str, *,
                        exclude_entity_id: str | None = None,
                        doc_type: str | None = None) -> tuple[str, str] | None:
    """(kind, number shown) for the Document or List ("doc" or "list") other than
    ``exclude_entity_id`` that shows, keeps or is stored under ``number``, both sides compared in
    their canonical form; None when the number is free. A number a record is stored under stays
    its own for good, so no other record takes it even after that record is renumbered. Documents
    and Lists share one number space, so every number is checked against this one rule.
    ``doc_type`` narrows the Documents that count to that type, for imported books, where another
    system's bill and invoice may carry the same number; every List still counts. The holder is
    named by the first number it has, without the spaces it may be stored with."""
    query = select(Projection.entity_type, Projection.state["ref_id"].as_string(),
                   Projection.state["doc_number"].as_string(), KEY_NUMBER).where(
        Projection.company_id == company_id,
        Projection.entity_type.in_(("doc", "list")),
        _number_match(canonical_number(sa.literal(strip_number(number), sa.Text))),
    )
    if exclude_entity_id:
        # A record may take back its own number, shown or stored under.
        query = query.where(Projection.entity_id != exclude_entity_id)
    if doc_type:
        query = query.where(or_(Projection.entity_type == "list",
                                Projection.state["doc_type"].as_string() == doc_type))
    row = (await session.execute(query.limit(1))).first()
    if row is None:
        return None
    kind, *numbers = row
    return kind, next(n for n in (strip_number(str(n or "")) for n in numbers) if n)


async def require_number_free(session: AsyncSession, company_id, number: object, *,
                              exclude_entity_id: str | None = None, doc_type: str | None = None) -> str:
    """Return ``number`` as a record stores it (strip_number) once no other
    Document or List holds it (number_holder); refuse it while one does, naming which kind holds
    it and the number as that record shows it. Every writer stores the returned number. The
    company lock serializes the check with every other numbering write. A number that is not
    text or is blank, or one no record can be stored under, is refused first."""
    if not isinstance(number, str) or not (number := strip_number(number)):
        raise HTTPException(status_code=422, detail=catalog_refusal("documents.err_number_required"))
    require_storable_number(number)
    await lock_company(session, company_id)
    holder = await number_holder(session, company_id, number, exclude_entity_id=exclude_entity_id,
                                 doc_type=doc_type)
    if holder is not None:
        kind, shown = holder
        raise HTTPException(status_code=409, detail=catalog_refusal(
            "documents.err_doc_number_taken" if kind == "doc" else "documents.err_list_number_taken", number=shown))
    return number


async def free_number(session: AsyncSession, company_id, number: object, **check) -> object:
    """``number`` as a record stores it: none stays none, a text blank once stripped is
    stored as empty and needs no check, and any other goes through require_number_free."""
    if number is None or isinstance(number, str) and not strip_number(number):
        return number and ""
    return await require_number_free(session, company_id, number, **check)


# The first numbers of a counter held_numbers reads; each further read doubles.
_WINDOW = 16


async def held_numbers(session: AsyncSession, company_id, settings: dict,
                       counters: Iterable[str]) -> frozenset[str]:
    """The numbers, as ``counters`` give them, that a Document or List holds among those the
    counters give next, as number_holder decides, read until each counter reaches a free one.
    The window doubles, so a long run of taken numbers costs a few indexed reads, never one per
    number."""
    held: set[str] = set()
    size, todo = _WINDOW, set(counters)
    while todo:
        windows = {counter: _candidates(settings, counter, size) for counter in todo}
        wanted = sa.values(sa.column("number", sa.Text), name="wanted").data(
            [(ref,) for ref in sorted({ref for refs in windows.values() for ref in refs})])
        # One join per number, so each candidate is read through that number's index.
        held.update((await session.execute(sa.union(*(select(wanted.c.number).join(Projection, sa.and_(
            Projection.company_id == company_id, Projection.entity_type.in_(("doc", "list")),
            number == canonical_number(wanted.c.number))) for number in _NUMBERS)))).scalars())
        todo = {counter for counter, refs in windows.items() if held.issuperset(refs)}
        size *= 2
    return frozenset(held)


def _candidates(settings: dict, counter: str, count: int) -> list[str]:
    """The ``count`` references ``counter`` gives from its next number, taken or not."""
    _seq, prefix, pattern, day, number = _counter(settings, counter)
    return [_expand_pattern(pattern, prefix, day, n) for n in range(number, number + count)]


async def claim_number(session: AsyncSession, company: Company, counter: str) -> str:
    """The next number of ``counter`` that no Document or List holds. A number a user, an
    import or another counter already gave is skipped, never reused, and nothing is
    renumbered. ``company`` comes from locked_company(), as for next_doc_ref()."""
    held = await held_numbers(session, company.id, dict(company.settings or {}), [counter])
    return next_doc_ref(company, counter, held)


async def peek_draft_ref(session: AsyncSession, company: Company, doc_type: str) -> str:
    """The reference the next new draft of ``doc_type`` would take, without taking it."""
    settings = dict(company.settings or {})
    counter = draft_counter(doc_type)
    return _first_free(settings, counter, await held_numbers(session, company.id, settings, [counter]))[0]


def _row_counter(doc_type: str, settings: dict) -> str | None:
    """The counter the Numbering row for ``doc_type`` shows, or None when nothing draws it:
    the transfer row shows the shared counter while transfers draw it, and the shared
    counter has its own row only while the company has it."""
    if doc_type == "list":
        return "list" if (settings.get("sequences") or {}).get("list") else None
    return list_sequence_key(doc_type, settings) if doc_type == "transfer" else doc_type


def _row_counters(settings: dict) -> dict[str, str]:
    """{doc type: the counter its Numbering row shows} for every counter something draws."""
    return {doc_type: counter for doc_type in _PREFIX_BY_DOC_TYPE if (counter := _row_counter(doc_type, settings))}


def get_all_sequences(company: Company, held: Collection[str]) -> list[dict]:
    """The numbering config of every counter something draws, one row per doc type, with the
    reference its next document takes past the numbers in ``held`` (held_numbers), or, when that
    number is too long to store, the refusal its next document meets."""
    settings = dict(company.settings or {})
    sequences = settings.get("sequences") or {}
    rows = []
    for doc_type, counter in _row_counters(settings).items():
        _seq, prefix, pattern, _day, _number = _counter(settings, counter)
        try:
            preview, refusal = _first_free(settings, counter, held)[0], None
        except HTTPException as e:
            preview, refusal = None, e.detail
        rows.append({"doc_type": doc_type, "prefix": prefix, "pattern": pattern,
                     "next": int((sequences.get(counter) or {}).get("next") or 1),
                     "preview": preview, "refusal": refusal})
    return rows


async def numbering_rows(session: AsyncSession, company: Company) -> list[dict]:
    """The Numbering tab's rows (get_all_sequences), each preview the number its next
    document gets."""
    settings = dict(company.settings or {})
    return get_all_sequences(company, await held_numbers(
        session, company.id, settings, set(_row_counters(settings).values())))


def _namespace(counter: str, seq: dict) -> str:
    """The numbers a counter can give, as its pattern with its prefix filled in."""
    pattern = str(seq.get("pattern") or DEFAULT_PATTERN)
    return pattern.replace("{PREFIX}", str(seq.get("prefix") or _PREFIX_BY_DOC_TYPE[counter]))


def update_sequence(company: Company, doc_type: str, prefix: str | None = None,
                    pattern: str | None = None, next_num: int | None = None) -> None:
    """Update numbering config for a single doc type.

    ``company`` must come from locked_company(), as for next_doc_ref()."""
    require_doc_type(doc_type)

    settings = dict(company.settings or {})
    sequences = dict(settings.get("sequences") or {})
    shared = sequences.get("list")
    # Saving the transfer row while transfers draw the shared counter gives them their own.
    split = doc_type == "transfer" and _row_counter(doc_type, settings) == "list"
    inherited = shared if split and not sequences.get(doc_type) else None
    seq = dict(sequences.get(doc_type) or inherited or {})
    if inherited is not None:
        # A split counter must not mint the same references as the shared one.
        seq["prefix"] = _PREFIX_BY_DOC_TYPE[doc_type]

    if prefix is not None:
        prefix = prefix.strip()
        if "{" in prefix or "}" in prefix:
            raise Refused("documents.err_numbering_prefix_braces")
        seq["prefix"] = prefix
    if pattern is not None:
        seq["pattern"] = require_pattern(pattern)
    if next_num is not None:
        # The number belongs to today's period, so the reset for a later period cannot discard it.
        day = _numbering_day(settings, seq)
        seq.update(next=max(1, next_num), year=day.year, month=day.month, day=day.day)

    # The counter's next number must be one a record can be stored under.
    _first_free({**settings, "sequences": {**sequences, doc_type: seq}}, doc_type, ())

    # Two counters with one format would give the same numbers, so the second is refused.
    target = _namespace(doc_type, seq)
    for kind in _PREFIX_BY_DOC_TYPE:
        counter = _row_counter(kind, settings)
        if counter and doc_type not in (kind, counter) and target == _namespace(counter, sequences.get(counter) or {}):
            raise Refused("documents.err_numbering_overlap", doc_type=kind)

    writes = {doc_type: seq}
    if split:
        writes["list"] = {**shared, "shares": [t for t in _shares(shared) if t != doc_type]}
    _store(company, writes)
