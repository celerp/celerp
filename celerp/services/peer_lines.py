# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Lines that came from outside the company, and the fund chosen for them.

A record an arrival writer brings in (a file import, a migration, a connected company's
document, an accountant's or a client's journal, a shared document, QuickBooks or Xero)
carries ``origin`` on its header and on each line it brought; a journal carries it on the
entry and every line. ``origin`` says where the money came from, so the fund it belongs to
is this company's choice: ``fund_choice`` (a fund id or ``"unrestricted"``) on the header
or on a line, with the line's ``fund_id`` to match.

One rule judges these keys on every write of a document's or a List's lines and header
(``judge_line_keys``, ``judge_header_keys``, run by the engine on every write):

- kept: a write that keeps a line (by ``line_id``) and omits a key keeps the stored value,
  so a writer that does not know these keys never drops them;
- ``origin`` is written only by an arrival writer (``emit_event(origin=)``) or a copy of
  stored lines; a different value is refused;
- ``fund_choice``, and ``fund_id`` on a line that came from outside or is linked to another
  record's line, change only with a ``FundChoiceGrant`` naming the line and the value. A
  grant is built only in this module, so the writers that may set a choice are the ones
  that call it: the Fund control (``require_fund_choices``) and a copy of stored lines
  (``granted_from_stored``);
- cleared (``keep_fund_choices``): a change to a marked line's amounts or to the document's
  money removes every choice on the record in the same write, and the writer is told;
- inbound (``inbound_keys``): where funds are on, data arriving from outside carries none of
  these keys; where they are off it is kept as sent, and turning funds on or off is judged
  against what unposted records carry (``records_carrying``).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from celerp.accounting_roles import catalog_refusal

FUND_KEYS = ("fund_choice", "fund_id")  # what a local choice sets on a line
LINE_KEYS = ("origin", *FUND_KEYS)
# fund_choice_reason: why an outside revision cleared the choices, until a user chooses again.
HEADER_KEYS = ("origin", "fund_choice", "fund_choice_source", "fund_choice_reason")
UNRESTRICTED = "unrestricted"  # the choice of no fund
# What a marked line's money is worked out from: a change to any of them clears the choices.
_LINE_MONEY_KEYS = ("item_id", "account", "quantity", "unit_price", "discount", "taxes")
# What an outside revision changed, named in the reason it clears the choices with
# (fund.choices_cleared_by_source.<change>): the lines, else the total, else the tax.
_LINE_CHANGES = {"item_id": "lines", "account": "lines", "taxes": "tax"}
_HEADER_CHANGES = {"doc_taxes": "tax", "tax_rate": "tax"}
# Where an outside record came from, each with its label (fund.origin.<origin>).
ORIGINS = frozenset({"file_import", "migration", "connected_company", "accountant", "client",
                     "shared_document", "accounting_system"})
# Arrivals read from a file, whose funds are chosen on the import screen.
_FILE_ORIGINS = frozenset({"file_import", "migration"})


_ABSENT = object()


@dataclass(frozen=True)
class FundChoiceGrant:
    """The fund keys one write may set: per line id the values it may carry, and the
    header's. Built only by the functions of this module."""
    lines: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    header: Mapping[str, Any] = field(default_factory=dict)


def granted_from_stored(lines: list, header: Mapping | None = None) -> FundChoiceGrant:
    """The grant of a copy of stored lines (a duplicate, a conversion, a subscription bill):
    each copied line keeps the origin and fund it carries, and the copy's header the source
    header's. A line the door sends without an id (a duplicate strips them) gets its new
    line id here, in place, so the grant can name it."""
    from celerp.services.document_lines import is_line_id, new_line_id

    granted = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        if not is_line_id(line.get("line_id")):
            line["line_id"] = new_line_id()
        keys = {key: line[key] for key in LINE_KEYS if key in line}
        if keys:
            granted[line["line_id"]] = keys
    return FundChoiceGrant(lines=granted, header={key: header[key] for key in HEADER_KEYS if key in (header or {})})


def checked(funds: Mapping, value, key: str, /, **params):
    """A choice as given when it is Unrestricted or an active fund of ``funds``; none is
    refused with ``key``."""
    if value == UNRESTRICTED:
        return value
    if value is None or value == "":
        raise _refused(422, key, **params)
    fund = funds.get(value)
    if fund is None:
        raise _refused(422, "posting.fund.unknown", fund=str(value))
    if fund.get("archived"):
        raise _refused(422, "posting.fund.archived", fund=fund.get("name", value))
    return value


async def require_fund_choices(session, company_id, record: Mapping, *, document: Any = _ABSENT,
                               every: Any = _ABSENT, lines: Mapping[str, Any],
                               source: str = "user") -> FundChoiceGrant:
    """The grant of a local user's choice on ``record``: ``document`` its document choice,
    ``lines`` a choice per marked line (by line id) and ``every`` the choice of each marked
    line ``lines`` does not name and that has none yet. Each value is ``"unrestricted"`` or an active fund of the
    company; a line's ``fund_id`` follows its choice (none for Unrestricted). ``source`` is
    who chose the document choice: the user, or a connection's standing default
    (``ConnectionDefault.source``)."""
    from celerp.services.journal_accounts import funds_by_id

    funds = await funds_by_id(session, company_id)

    numbered = {li.get("line_id"): (n, li) for n, li in enumerate(record.get("line_items") or [], 1)
                if isinstance(li, dict)}
    for line_id in lines:
        if line_id not in numbered:
            raise _refused(422, "posting.fund.choice_line_unknown")
        n, line = numbered[line_id]
        if not line.get("origin"):
            raise _refused(422, "posting.fund.choice_local_line", line=n)
    granted = {}
    for line_id, (n, line) in numbered.items():
        if line_id in lines:
            value = checked(funds, lines[line_id], "posting.fund.choice_missing", line=n)
        elif every is not _ABSENT and line.get("origin") and not line.get("fund_choice"):
            value = checked(funds, every, "posting.fund.choice_missing", line=n)
        else:
            continue
        granted[line_id] = {"fund_choice": value, "fund_id": None if value == UNRESTRICTED else value}
    header = {"fund_choice_reason": None} if record.get("fund_choice_reason") else {}
    if document is not _ABSENT:
        header.update(fund_choice=checked(funds, document, "posting.fund.document_choice_missing"),
                      fund_choice_source=source)
    return FundChoiceGrant(lines=granted, header=header)


# Company setting: the standing fund per connection, keyed "<origin>:<source key>", each
# {fund_choice, set_by, set_at, version, request_key}; a cleared default keeps its entry so
# its version keeps counting.
CONNECTION_DEFAULTS = "fund_connection_defaults"


def default_key(origin: str, source_key: str) -> str:
    return f"{origin}:{source_key}"


@dataclass(frozen=True)
class ConnectionDefault:
    """An active standing default: the fund records from one connection take at their first
    arrival, the local user who set it (who posts what it fills) and the tag it leaves."""
    fund: str
    set_by: Any
    role: str
    source: str


def default_active(entry: Mapping | None, settings: Mapping, role: str, funds: Mapping) -> bool:
    """A stored default applies while it names a fund that still takes postings (or
    Unrestricted), funds are in use and its setter still holds the fund permission."""
    from celerp.services.journal_accounts import funds_enabled
    from celerp.services.permissions import role_has_permission

    choice = (entry or {}).get("fund_choice")
    return bool(choice and funds_enabled(dict(settings))
                and (choice == UNRESTRICTED or (choice in funds and not funds[choice].get("archived")))
                and role_has_permission(dict(settings), role, "manage_accounting"))


async def connection_default(session, company_id, origin: str, source_key: str) -> ConnectionDefault | None:
    """The active standing default of the connection (``origin``, ``source_key``), or None:
    records from it then wait for a local user's choice."""
    import uuid

    from sqlalchemy import select

    from celerp.models.company import Company
    from celerp.services.journal_accounts import funds_by_id
    from celerp.services.permissions import read_authority

    settings = await session.scalar(select(Company.settings).where(Company.id == company_id)) or {}
    entry = (settings.get(CONNECTION_DEFAULTS) or {}).get(default_key(origin, source_key))
    if not (entry or {}).get("fund_choice"):
        return None
    set_by = uuid.UUID(entry["set_by"])
    role, settings = await read_authority(session, company_id, set_by)
    if not default_active(entry, settings, role, await funds_by_id(session, company_id)):
        return None
    return ConnectionDefault(entry["fund_choice"], set_by, role, f"default:{default_key(origin, source_key)}")


def chosen_lines(lines: list, grant: FundChoiceGrant) -> list:
    """``lines`` with the choices ``grant`` names applied; Unrestricted writes ``fund_id``
    as none, so a fund chosen before does not stay as an omitted, kept key (LK1)."""
    return [{**line, **grant.lines[line["line_id"]]}
            if isinstance(line, dict) and line.get("line_id") in grant.lines else line for line in lines]


async def chosen_journal_lines(session, company_id, entries: list[dict], *, every: str | None,
                               lines: Mapping[str, str | None], source: str = "user"):
    """The fund keys each line of a journal takes (in order, only those with a value) from
    the choices ``every`` (the entry's, and of each line ``lines`` does not name) and ``lines``
    (line number from 1, as text -> choice; none is no choice), and the grant that lets the
    entry carry them. ``entries`` carry their ``origin``."""
    numbered = [{**line, "line_id": str(n)} for n, line in enumerate(entries, 1)]
    grant = await require_fund_choices(
        session, company_id, {"line_items": numbered}, lines={n: v for n, v in lines.items() if v is not None},
        source=source, **({"document": every, "every": every} if every is not None else {}))
    return [{key: line[key] for key in FUND_KEYS if line.get(key)} for line in chosen_lines(numbered, grant)], grant


class ImportFundChoices(BaseModel):
    """The funds an import screen chose for the records of one batch: ``all`` each record's
    choice, ``records`` (record id -> choice) a record's own, ``lines`` ("<record id>:<line
    number from 1>" -> choice) a line's own. A choice is a fund id or "unrestricted"."""
    all: str | None = None
    records: dict[str, str] = Field(default_factory=dict)
    lines: dict[str, str] = Field(default_factory=dict)

    def of(self, record: str) -> tuple[str | None, dict[str, str]]:
        """``record``'s choice and its lines' own choices, by line number (as text)."""
        prefix = f"{record}:"
        return (self.records.get(record, self.all),
                {key[len(prefix):]: value for key, value in self.lines.items() if key.startswith(prefix)
                 and key[len(prefix):].isdigit()})


async def check_import_fund_choices(session, company_id, choices: ImportFundChoices | None,
                                    records: Mapping[str, int]) -> ImportFundChoices:
    """The batch's choices once they can apply, else refused before any record is written:
    while funds are off, naming a record the batch does not carry (``records``: record id ->
    its number of lines) or a line by anything but a number the record has, or a value that is
    not Unrestricted or an active fund of the company."""
    from celerp.services.journal_accounts import funds_by_id, funds_enabled, hold_fund_ledger

    choices = choices or ImportFundChoices()
    if choices.all is None and not choices.records and not choices.lines:
        return choices
    if not funds_enabled(await hold_fund_ledger(session, company_id)):
        raise _refused(422, "posting.fund.import_funds_off")
    for record in [*choices.records, *(key.rpartition(":")[0] for key in choices.lines)]:
        if record not in records:
            raise _refused(422, "posting.fund.choice_record_unknown", record=record)
    for key in choices.lines:
        record, _, line = key.rpartition(":")
        if not line.isdigit() or not 1 <= int(line) <= records[record]:
            raise _refused(422, "posting.fund.import_line_unknown", record=record, line=line)
    funds = await funds_by_id(session, company_id)
    for value in [choices.all, *choices.records.values(), *choices.lines.values()]:
        if value is not None:
            checked(funds, value, "posting.fund.document_choice_missing")
    return choices


def _fund_of(cover) -> str | None:
    """The fund a choice (or a local line's ``fund_id``) gives; none for Unrestricted or no choice."""
    return None if cover in (_ABSENT, None, "", UNRESTRICTED) else cover


def _covers(doc: Mapping) -> tuple[dict[int, Any], Any]:
    """What covers each line of ``doc`` (by line number, 1-based) and what no line stands
    behind: a line that came from outside its own choice, else the document's, else nothing
    (``_ABSENT``); a local line its ``fund_id``. The document's choice covers the header of a
    record that came from outside; a local header needs none."""
    document = doc.get("fund_choice") or (_ABSENT if doc.get("origin") else None)
    covers = {}
    for n, line in enumerate(doc.get("line_items") or [], 1):
        if not isinstance(line, dict):
            covers[n] = None
        elif line.get("origin"):
            covers[n] = line.get("fund_choice") or doc.get("fund_choice") or _ABSENT
        else:
            covers[n] = line.get("fund_id")
    return covers, document


def leg_funds(doc: Mapping) -> tuple[dict[int, str | None], str | None]:
    """The fund of each line of ``doc`` (by line number) its entry posts to, and the fund of
    what no line stands behind (_covers); a line nothing covers has none, and rule R
    (check_outside_record) refuses its entry."""
    covers, document = _covers(doc)
    return {n: _fund_of(cover) for n, cover in covers.items()}, _fund_of(document)


_SWAPPED = {"debit": "credit", "credit": "debit", "fx_debit": "fx_credit", "fx_credit": "fx_debit"}


def _posted_keys(line: Mapping) -> dict:
    """The fund keys a copy of a posted line carries (rule V): its origin, fund and choice. A
    line that came from outside and posted with no choice (before funds were in use) takes
    the fund it posted with, or Unrestricted, since the posted entry is the authority."""
    keys = {key: line[key] for key in LINE_KEYS if line.get(key)}
    if line.get("origin") and not line.get("fund_choice"):
        keys["fund_choice"] = line.get("fund_id") or UNRESTRICTED
    return keys


def copy_posted_lines(entry_id: str, entries: list, *, reverse: bool) -> list[dict]:
    """The lines of posted entry ``entry_id`` written again (``reverse`` swaps debit and
    credit), each keeping its fund keys (_posted_keys) and naming the entry it copies in
    ``posted_from``, so rule R lets it through without a new choice (check_outside_record). A line
    naming a contact carries the gift classification it posted with (journal_accounts.
    _stamp_named_gifts), False when it posted without one, so no copy is classified again."""
    out = []
    for line in entries or []:
        if isinstance(line, dict):
            copied = {(_SWAPPED.get(k, k) if reverse else k): v for k, v in line.items()}
            gift = {"gift": line.get("gift") is True} if line.get("contact") else {}
            out.append({**copied, **gift, **_posted_keys(line), "posted_from": entry_id})
    return out


def posted_line_funds(lines: list, posted_doc: Mapping) -> list:
    """``lines`` with each line linked to a line of ``posted_doc`` (``source_line_id``) carrying
    that line's origin and the fund it posted with (its own choice, else the document's: leg_funds)
    in place of its own keys: a credit or debit note line reverses the fund the line it
    credits posted with, never one sent with it."""
    covers, _ = _covers(posted_doc)
    posted = {li.get("line_id"): (n, li) for n, li in enumerate(posted_doc.get("line_items") or [], 1)
              if isinstance(li, dict) and li.get("line_id")}
    out = []
    for line in lines or []:
        n, source = posted.get(line.get("source_line_id"), (None, None)) if isinstance(line, dict) else (None, None)
        if source is not None:
            fund = _fund_of(covers[n])
            keys = {**({"origin": source["origin"], "fund_choice": fund or UNRESTRICTED} if source.get("origin") else {}),
                    **({"fund_id": fund} if fund else {})}
            line = {**{k: v for k, v in line.items() if k not in LINE_KEYS}, **keys}
        out.append(line)
    return out


async def _copied_legs(session, company_id, data: Mapping, legs: list[dict]) -> set[int]:
    """Which of ``legs`` (by position) are copies of a leg the same record posted: a
    ``posted_from`` naming an entry of that record, of a record the entry ``settles`` or the
    entry ``data`` reverses, that posted a line with the same fund (a settlement moves the
    fund's share through a bank or exchange account its source never touched)."""
    from celerp.models.projections import Projection

    found: dict[str, Mapping | None] = {}
    copied = set()
    for i, line in enumerate(legs):
        source_id = line.get("posted_from")
        if not source_id:
            continue
        if source_id not in found:
            row = await session.get(Projection, {"company_id": company_id, "entity_id": source_id})
            state = (row.state or {}) if row is not None and row.entity_type == "journal_entry" else {}
            records = {data.get("record_id"), *(data.get("settles") or [])} - {None}
            same = state.get("record_id") in records or source_id == data.get("reverses")
            found[source_id] = state if same and state.get("status") in ("posted", "void") else None
        source = found[source_id]
        if source is not None and any((e.get("fund_id") or None) == (line.get("fund_id") or None)
                                      for e in source.get("entries") or []):
            copied.add(i)
    return copied


async def check_outside_record(session, company_id, data: Mapping, entries: list[dict], settings: Mapping) -> None:
    """Rule R: a new entry for a record that came from outside posts only lines a fund choice
    covers. A record's entry (``data["record_id"]``) names on each line the document lines
    behind it (``source_lines``, fund_legs); a line with none stands for the header. A journal
    that arrived from outside (``data["origin"]``) is its own record, each line covered by its
    own choice or the entry's. A line is covered when every line behind it has a choice and
    the line carries the fund that choice gives; stock and stock cost lines carry no fund and
    need none, nor does a stock line (one whose own legs all go to stock) on the entry's
    other lines (the payable for goods added to stock), and a copy of a line the record posted (_copied_legs) keeps the fund it posted
    with. Anything else raises FundChoiceNeeded naming the lines to choose for."""
    from celerp.models.projections import Projection
    from celerp.services.journal_accounts import FundChoiceNeeded, stock_codes

    if data.get("origin"):
        document = data.get("fund_choice") or _ABSENT
        covers = {n: line.get("fund_choice") or document for n, line in enumerate(entries, 1)}
        legs = [(line, [n]) for n, line in enumerate(entries, 1)]
        record, origin = str(data.get("memo") or ""), data["origin"]
    else:
        row = await session.get(Projection, {"company_id": company_id, "entity_id": data.get("record_id")}) \
            if data.get("record_id") else None
        state = (row.state or {}) if row is not None else {}
        lines = [li for li in state.get("line_items") or [] if isinstance(li, dict)]
        origin = state.get("origin") or next((li["origin"] for li in lines if li.get("origin")), None)
        if not origin:
            return
        covers, document = _covers(state)
        legs = [(line, line.get("source_lines") or []) for line in entries]
        record = str(state.get("ref_id") or data["record_id"].split(":", 1)[-1])
    stock = stock_codes(dict(settings))
    # A stock line is one whose own legs (those behind it alone) all go to stock; a leg spread
    # over several lines (carriage, tax, the payable) says nothing about any one of them.
    own: dict[int, list[bool]] = {}
    for line, numbers in legs:
        if len(numbers) == 1:
            own.setdefault(numbers[0], []).append(line["account"] in stock)
    stock_lines = {n for n, flags in own.items() if all(flags)}
    copied = await _copied_legs(session, company_id, data, entries)
    uncovered: set[int] = set()
    refused = False
    for i, (line, numbers) in enumerate(legs):
        if line["account"] in stock or i in copied:
            continue
        fund = line.get("fund_id") or None
        if not numbers:
            refused |= document is _ABSENT or _fund_of(document) != fund
        bad = {n for n in numbers if n not in stock_lines
               and (covers.get(n, _ABSENT) is _ABSENT or _fund_of(covers[n]) != fund)}
        uncovered |= bad
        refused |= bool(bad)
    if refused:
        raise FundChoiceNeeded(record, source_label(origin), sorted(uncovered))


def copied_header(state: Mapping) -> dict:
    """The fund keys of a record's header a copy of it carries."""
    return {key: state[key] for key in HEADER_KEYS if key in state}


def origin_key(origin) -> str:
    """The catalog key naming where a record came from; an origin this version does not know
    reads as the neutral "another app"."""
    return f"fund.origin.{origin if origin in ORIGINS else 'unknown'}"


def source_label(origin) -> dict:
    """Where a record came from (origin_key), as a refusal param."""
    return catalog_refusal(origin_key(origin))


def _refused(status: int, key: str, /, **params) -> HTTPException:
    return HTTPException(status_code=status, detail=catalog_refusal(key, **params))


def _marked_or_linked(line: dict) -> bool:
    return bool(line.get("origin") or line.get("source_line_id"))


def judge_line_keys(stored_lines, line_set: list, *, origin: str | None, grant: FundChoiceGrant | None) -> None:
    """Apply the line-key rule to a write's line set (after normalize_line_ids), in place.

    A kept line that omits a key gets the stored value. An arrival write (``origin``) marks
    every line that carries no origin and keeps an inbound value as sent. Any other write
    may not change ``origin`` (409), may not put it on a line it adds (422), and changes
    ``fund_choice``, or ``fund_id`` on a marked or linked line, only as ``grant`` names it
    (409)."""
    stored = {li["line_id"]: li for li in stored_lines or [] if isinstance(li, dict) and li.get("line_id")}
    granted = grant.lines if grant is not None else {}
    for n, line in enumerate(line_set, 1):
        if not isinstance(line, dict):
            continue
        old = stored.get(line.get("line_id"))
        allowed = granted.get(line.get("line_id"), {})
        if old is not None:
            for key in LINE_KEYS:
                if key not in line and key in old:
                    line[key] = old[key]
        if origin is not None:
            line.setdefault("origin", origin)
            continue
        before = old or {}
        if line.get("origin") != before.get("origin") and allowed.get("origin", _ABSENT) != line.get("origin"):
            if before.get("origin"):
                raise _refused(409, "posting.fund.origin_fixed", source=source_label(before["origin"]))
            raise _refused(422, "posting.fund.key_not_taken", field="origin")
        for key in FUND_KEYS:
            if line.get(key) == before.get(key) or allowed.get(key, _ABSENT) == line.get(key):
                continue
            if key == "fund_id" and not _marked_or_linked(line):
                continue  # a local line's fund is the writer's, judged by the fund rules at posting
            if not line.get("origin") and key not in before:
                raise _refused(422, "posting.fund.key_not_taken", field=key)
            raise _refused(409, "posting.fund.choice_not_granted", line=n)


def judge_header_keys(stored: Mapping, written: dict, *, origin: str | None, grant: FundChoiceGrant | None) -> dict:
    """The header fund keys a write sets (``written``: field -> value), judged as lines are.
    Returns the keys to add to the write: ``origin`` when an arrival write reaches a record
    that has none and brings none."""
    if origin is not None:
        return {} if stored.get("origin") or written.get("origin") else {"origin": origin}
    granted = grant.header if grant is not None else {}
    for key in HEADER_KEYS:
        if key not in written or written[key] == stored.get(key) or granted.get(key, _ABSENT) == written[key]:
            continue
        if key == "origin":
            if stored.get("origin"):
                raise _refused(409, "posting.fund.document_origin_fixed", source=source_label(stored["origin"]))
            raise _refused(422, "posting.fund.key_not_taken", field="origin")
        raise _refused(409, "posting.fund.document_choice_not_granted")
    return {}


def inbound_keys(stored_lines, line_set, stored: Mapping, header: Mapping) -> list[tuple[str, int | None]]:
    """The fund keys an arrival brings with it, each with the line it is on (from 1; none
    for the header): on a line, a value other than the stored line's (matched by line id),
    and on the header a value other than the stored one. A value of none is no value."""
    kept = {li["line_id"]: li for li in stored_lines or [] if isinstance(li, dict) and li.get("line_id")}
    found = [(key, None) for key in HEADER_KEYS
             if header.get(key) is not None and header.get(key) != stored.get(key)]
    for n, line in enumerate(line_set or (), 1):
        if isinstance(line, dict):
            before = kept.get(line.get("line_id"), {})
            found += [(key, n) for key in LINE_KEYS if line.get(key) is not None and line.get(key) != before.get(key)]
    return found


def journal_inbound_keys(data: Mapping, grant: FundChoiceGrant | None) -> list[tuple[str, int | None]]:
    """The fund keys an arrival journal brings (inbound_keys) other than the values ``grant``
    names: a line by its number in the entry (from 1, as text), the header as itself. A line
    copying one these books posted (``posted_from``) keeps its own keys."""
    header, lines = (grant.header, grant.lines) if grant is not None else ({}, {})
    found = [(key, None) for key in HEADER_KEYS
             if data.get(key) is not None and header.get(key, _ABSENT) != data.get(key)]
    for n, line in enumerate(data.get("entries") or [], 1):
        if isinstance(line, dict) and not line.get("posted_from"):
            allowed = lines.get(str(n), {})
            found += [(key, n) for key in LINE_KEYS
                      if line.get(key) is not None and allowed.get(key, _ABSENT) != line.get(key)]
    return found


def refuse_inbound(found: list[tuple[str, int | None]], origin: str) -> None:
    """Where funds are on, the fund keys are this company's own: an arrival bringing one is
    refused naming it and its line; a file is told the fund is chosen on the import screen."""
    if found:
        key, line = found[0]
        if origin in _FILE_ORIGINS:
            raise _refused(422, "posting.fund.file_key", field=key)
        if line is not None:
            raise _refused(422, "posting.fund.inbound_line_key", field=key, line=line)
        raise _refused(422, "posting.fund.inbound_key", field=key)


def _measure(values: Mapping, key: str):
    """A money input as compared for a change: an empty or zero value is no value."""
    value = values.get(key)
    return None if value in (None, "", 0, [], {}) else value


def keep_fund_choices(stored: Mapping, line_set: list | None, written: Mapping, *,
                      source: str | None = None) -> tuple[list | None, dict]:
    """Clear the record's fund choices when this write changes its money (LK5): a kept marked
    line's amounts (``_LINE_MONEY_KEYS``) or a header money input in ``written`` (the money
    inputs the write sets, celerp_docs.doc_money.DOC_MONEY_INPUTS, as the engine reads them). Every
    ``fund_choice`` and the ``fund_id`` of every marked line go, in the same write. A revision
    from outside (``source``, the arrival's origin) also changes the lines when it adds or
    removes one, and its result names the change as ``reason``, the refusal stored on the
    record until a user chooses again. Returns the line set to write (the stored lines cleared
    when the write carries none) and what was cleared, ``{"lines": [line numbers], "document":
    bool, "reason": refusal}``; ``(line_set, {})`` when nothing was."""
    stored_lines = [li for li in stored.get("line_items") or [] if isinstance(li, dict)]
    held = any(li.get("fund_choice") for li in stored_lines) or bool(stored.get("fund_choice"))
    if not held:
        return line_set, {}
    before = {li.get("line_id"): li for li in stored_lines if li.get("origin")}
    changes = {_HEADER_CHANGES.get(key, "total") for key in written
               if key != "line_items" and _measure(written, key) != _measure(stored, key)}
    for line in line_set or ():
        old = before.get(line.get("line_id")) if isinstance(line, dict) else None
        changes |= {_LINE_CHANGES.get(k, "total") for k in _LINE_MONEY_KEYS
                    if old is not None and _measure(line, k) != _measure(old, k)}
    if source is not None and line_set is not None and (
            [li.get("line_id") for li in line_set if isinstance(li, dict)] != [li.get("line_id") for li in stored_lines]):
        changes.add("lines")
    if not changes:
        return line_set, {}
    lines = [dict(li) for li in stored_lines] if line_set is None else line_set
    cleared = []
    for n, line in enumerate(lines, 1):
        if isinstance(line, dict) and line.pop("fund_choice", None):
            cleared.append(n)
        if isinstance(line, dict) and line.get("origin"):
            line.pop("fund_id", None)
    result = {"lines": cleared, "document": bool(stored.get("fund_choice"))}
    if source is not None:
        # A price change moves a line's tax amount too: the tax is named only when it alone changed.
        change = next(c for c in ("lines", "total", "tax") if c in changes)
        result["reason"] = catalog_refusal(f"fund.choices_cleared_by_source.{change}", source=source_label(source))
    return lines, result


async def records_carrying(session, company_id, keys: tuple[str, ...]) -> list[str]:
    """The numbers of the unposted records whose header or a line carries a value for one of
    ``keys``: draft documents, and Lists (which never post) not yet converted or voided."""
    from sqlalchemy import select

    from celerp.models.projections import Projection

    rows = await session.execute(select(Projection.entity_id, Projection.entity_type, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type.in_(("doc", "list"))))
    found = []
    for entity_id, entity_type, state in rows:
        state = state or {}
        unposted = state.get("status") == "draft" if entity_type == "doc" else state.get("status") not in ("converted", "void")
        lines = [li for li in state.get("line_items") or [] if isinstance(li, dict)]
        if unposted and any(part.get(key) is not None for part in (state, *lines) for key in keys):
            found.append(str(state.get("ref_id") or entity_id.split(":", 1)[-1]))
    return sorted(found)


def outside_fund_needs(doc: Mapping) -> list[int | None]:
    """What of ``doc`` a fund choice must still cover before its entry posts (_covers): each
    line from outside with no choice of its own or the document's, by number, and None for a
    record from outside with no lines and no document choice (its whole total is header)."""
    covers, document = _covers(doc)
    needs: list[int | None] = [n for n, cover in covers.items() if cover is _ABSENT]
    if not covers and document is _ABSENT:
        needs.append(None)
    return needs


def waits_for_funds(fix) -> bool:
    """Whether a held item's ``fix`` waits for a local user's fund choices: a journal's
    (``funds``) or a connected company's document's (``doc_funds``)."""
    from celerp.services.peer_documents import DOC_FUNDS

    return (fix or {}).get("kind") in ("funds", DOC_FUNDS)


def held_accounts(fix: Mapping) -> list[str | None]:
    """The account each line of a held item posts to, numbered as a choice names it (from 1):
    a journal's line account, a document line's purchasing account (None where the line
    names none and its account follows from the document)."""
    if fix.get("kind") == "funds":
        return [line.get("account") for line in fix.get("entries") or [] if isinstance(line, dict)]
    return [line.get("account_code") for line in (fix.get("sent") or {}).get("line_items") or []
            if isinstance(line, dict)]


async def outside_waiting(session, company_id) -> dict:
    """The outside records waiting for a fund choice in a company with funds in use: ``held``,
    the items a connected company sent that wait for funds (a ``funds`` or ``doc_funds`` fix,
    posted from Sync accounts), and ``documents``, the documents that post (on finalize or on
    receipt) with no posted entry of their own and a leg no choice covers
    (outside_fund_needs). ``count`` is both together; all zero with funds off."""
    from sqlalchemy import select

    from celerp.models.company import Company
    from celerp.models.company_peer import CompanyPeerAttention
    from celerp.models.projections import Projection
    from celerp.services.auto_je import IMPORT_TREATMENT
    from celerp.services.journal_accounts import funds_enabled
    from celerp.modules.boundary import is_running

    company = await session.get(Company, company_id)
    if company is None or not funds_enabled(dict(company.settings or {})):
        return {"count": 0, "held": 0, "documents": []}
    fixes = (await session.scalars(select(CompanyPeerAttention.fix).where(
        CompanyPeerAttention.company_id == company_id))).all()
    held = sum(1 for fix in fixes if waits_for_funds(fix))
    if not is_running("celerp-docs"):  # documents wait only where Documents can open them
        return {"count": held, "held": held, "documents": []}
    from celerp_docs.doc_constants import FUND_DOC_TYPES

    rows = await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_(sorted(FUND_DOC_TYPES)),
        Projection.state["status"].as_string().notin_(("void", "converted"))))
    needing = {entity_id: state for entity_id, state in rows
               if (state or {}).get(IMPORT_TREATMENT) != "opening_balances" and outside_fund_needs(state or {})}
    if needing:
        posted = set((await session.scalars(select(Projection.state["record_id"].as_string()).where(
            Projection.company_id == company_id, Projection.entity_type == "journal_entry",
            Projection.state["record_id"].as_string().in_(needing),
            Projection.state["status"].as_string() == "posted"))).all())
        needing = {k: v for k, v in needing.items() if k not in posted}
    documents = sorted(needing)
    return {"count": held + len(documents), "held": held, "documents": documents}


def mark_journal(data: dict, origin: str) -> None:
    """An arrival journal: the entry and every line carry ``origin`` (an inbound value stays as sent)."""
    data.setdefault("origin", origin)
    for line in data.get("entries") or []:
        if isinstance(line, dict):
            line.setdefault("origin", origin)


# ── Change fund on a posted outside record ──────────────────────────────────────────────

# What a posted leg holds besides what it is: its amounts, its fund and what it was built from.
_LEG_AMOUNTS = ("debit", "credit", "fx_debit", "fx_credit")
_LEG_FUND = ("fund_id", "fund_choice", "posted_from")


def _merged_legs(legs: list[dict], key) -> list[dict]:
    """``legs`` merged where ``key`` is the same, in first-seen order: debit and credit (and
    the fx pair) netted, ``source_lines`` joined."""
    from decimal import Decimal

    from celerp.services.money import to_decimal, to_stored_float

    groups: dict[str, list[dict]] = {}
    for leg in legs:
        groups.setdefault(json.dumps(key(leg), sort_keys=True, default=str), []).append(leg)
    out = []
    for same in groups.values():
        line = {k: v for k, v in same[0].items() if k not in (*_LEG_AMOUNTS, "source_lines")}
        for dr, cr in (("debit", "credit"), ("fx_debit", "fx_credit")):
            if any(dr in leg or cr in leg for leg in same):
                net = sum((to_decimal(leg.get(dr) or 0) - to_decimal(leg.get(cr) or 0) for leg in same), Decimal(0))
                line[dr], line[cr] = to_stored_float(max(net, Decimal(0))), to_stored_float(max(-net, Decimal(0)))
        if any("source_lines" in leg for leg in same):
            line["source_lines"] = sorted({n for leg in same for n in leg.get("source_lines") or ()})
        out.append(line)
    return out


def document_relabel(doc: Mapping, settings: Mapping, currency: str):
    """How Change fund writes a document's posted entries again on the funds ``doc`` now
    carries (repost_record_postings ``relabel``), accounts and amounts as posted: the legs
    are merged back by account and the lines behind them, then carried again by fund_legs
    (rule LG): a leg takes the fund of the lines behind it, split by their gross shares
    (bill_line_charges) when several stand behind it, a leg no line stands behind the
    document's fund, the receivable or payable each fund's balancing figure, a stock or cost
    leg no fund (rule 7); last, legs of one account and fund are regrouped."""
    from celerp.accounting_roles import AccountRole
    from celerp.services.account_roles import scope_codes
    from celerp.services.auto_je import bill_line_charges
    from celerp.services.journal_accounts import CONTROL, HEADER, fund_legs, stock_codes

    funds, header = leg_funds(doc)
    gross = {n: amount for n, (amount, _, _) in enumerate(bill_line_charges(dict(doc)), 1)}
    stock = stock_codes(dict(settings))
    control = scope_codes(dict(settings), AccountRole.RECEIVABLE) | scope_codes(dict(settings), AccountRole.PAYABLE)

    def kind(leg: Mapping):
        if leg["account"] in stock:
            return None
        if leg["account"] in control:
            return CONTROL
        return tuple(leg.get("source_lines") or ()) or HEADER

    def relabel(lines: list[dict]) -> list[dict]:
        bare = [{k: v for k, v in line.items() if k not in _LEG_FUND} for line in lines]
        legs = _merged_legs(bare, lambda leg: [kind(leg), {k: v for k, v in leg.items()
                                                            if k not in (*_LEG_AMOUNTS, "source_lines")}])
        bases = [{n: gross.get(n, 0) for n in basis} if isinstance(basis, tuple) else basis
                 for basis in map(kind, legs)]
        if bases.count(CONTROL) > 1:
            raise RuntimeError("A document entry with more than one receivable or payable line cannot be relabelled")
        carried = fund_legs([{k: v for k, v in leg.items() if k != "source_lines"} if kind(leg) is None else leg
                             for leg in legs], bases, funds, header, currency)
        return _merged_legs(carried, lambda leg: {k: v for k, v in leg.items()
                                                  if k not in (*_LEG_AMOUNTS, "source_lines")})
    return relabel


class ChangeFundIn(BaseModel):
    """A Change fund request: the new choices (``lines`` by line id on a document, by line
    number on a journal; a fund id or "unrestricted"), why, the record's version it was made
    from and the key that makes a repeat answer with the first result."""
    expected_version: int
    request_key: str = Field(min_length=1)
    reason: str
    lines: dict[str, str] = Field(default_factory=dict)


def _choices_of(state: Mapping, numbered: bool) -> dict:
    """The fund choices a record carries: its own and each marked line's (by line id, or by
    number for a journal)."""
    lines = state.get("entries" if numbered else "line_items") or []
    return {"document": state.get("fund_choice"),
            "lines": {(str(n) if numbered else line.get("line_id")): line.get("fund_choice")
                      for n, line in enumerate(lines, 1) if isinstance(line, dict) and line.get("origin")}}


async def change_posted_fund(session, company_id, record_id: str, change: ChangeFundIn, actor, *,
                             journal: bool, document: Any = _ABSENT) -> dict:
    """Change fund on a posted record that came from outside: a document (``document`` its
    choice, ``change.lines`` by line id) or an outside journal (``journal``; ``document`` the
    choice of the entry and every line ``change.lines`` does not name, lines by number).

    Checked in order, each refusal writing nothing: funds in use, the actor's
    manage_accounting under the lock, the repeat (the same change again under its request key
    answers with its first result, any other change under it is refused: keyed_event), the reason, the version, the values, a record
    carrying ``origin``, posted, not settled by a payment or note, not held by the opening
    balances, every entry it voids in an open period; and, after the work below, that it
    moves some amount to another fund (a document's own choice does not move a line that has
    its own). Then, inside one fund_change_action:
    the document's choices are written, its entries voided as one batch naming the copies
    replacing them and posted again on the new funds (document_relabel), and the modules
    keeping a record that copied a fund from it move it (doc_posting_dependents); a journal is
    voided and posted again as ``{je_id}:fund:{n}``. The action is judged once on leaving
    it; ``doc.fund_changed`` (``acc.journal_entry.fund_changed``) records the change last."""
    from sqlalchemy import func, select

    from celerp.events.engine import _check_period_lock, emit_event, keyed_event, request_fingerprint
    from celerp.models.company import Company
    from celerp.models.projections import Projection
    from celerp.services import auto_je, doc_posting_dependents
    from celerp.services.company_lock import lock_fund_ledger, lock_projections
    from celerp.services.je_keys import je_void_data
    from celerp.services.journal_accounts import fund_change_action, funds_by_id, funds_enabled
    from celerp.services.permissions import locked_authority

    await lock_fund_ledger(session, company_id, shared=False)
    settings = await session.scalar(select(Company.settings).where(Company.id == company_id)) or {}
    if not funds_enabled(settings):
        raise _refused(422, "fund.not_enabled")
    await locked_authority(session, company_id, actor, ("manage_accounting",))
    key = f"fundchange:{record_id}:{change.request_key}"
    request = {**change.model_dump(mode="json"), "journal": journal,
               "document": None if document is _ABSENT else ["chosen", document]}
    if (stored := await keyed_event(session, company_id, key, request)) is not None:
        return dict(stored.data.get("result") or {})
    reason = change.reason.strip()
    if not reason:
        raise _refused(422, "posting.fund.change_reason_missing")
    row = (await lock_projections(session, company_id, [record_id])).get(record_id)
    if row is None or row.entity_type != ("journal_entry" if journal else "doc"):
        raise _refused(404, "acct.err_je_not_found" if journal else "documents.document_not_found")
    if row.version != change.expected_version:
        raise HTTPException(status_code=409, detail={**catalog_refusal("posting.fund.change_stale"),
                                                     "code": "stale_version"})
    state = dict(row.state or {})
    if document is _ABSENT and not change.lines:
        raise _refused(422, "posting.fund.choice_empty")
    if document not in (_ABSENT, UNRESTRICTED) and document not in await funds_by_id(session, company_id):
        raise _refused(422, "posting.fund.document_choice_missing")

    if journal:
        bare = [{k: v for k, v in line.items() if k not in FUND_KEYS} for line in state.get("entries") or []
                if isinstance(line, dict)]
        keys, grant = await chosen_journal_lines(session, company_id, bare, lines=change.lines,
                                                 every=None if document is _ABSENT else document)
        if not state.get("origin"):
            raise _refused(422, "posting.fund.change_not_outside")
        if state.get("status") != "posted":
            raise _refused(422, "posting.fund.change_not_posted")
        dates = [state.get("ts")]
    else:
        await require_fund_choices(session, company_id, state, lines=change.lines,
                                   **({} if document is _ABSENT else {"document": document}))
        if not (state.get("origin") or any(isinstance(li, dict) and li.get("origin")
                                           for li in state.get("line_items") or [])):
            raise _refused(422, "posting.fund.change_not_outside")
        copies = await auto_je.restore_ids(session, company_id, record_id, goods_movements=True)
        if not copies:
            raise _refused(422, "posting.fund.change_not_posted")
        if float(state.get("amount_paid") or 0) or float(state.get("credited") or 0):
            raise _refused(422, "posting.fund.change_settled")
        name = str(state.get("ref_id") or record_id)
        if await auto_je.imported_carriage(session, company_id, record_id):
            raise _refused(422, "posting.fund.change_opening_held", document=name)
        prefix = f"je:auto:{record_id}:"
        dates = list((await session.scalars(select(Projection.state["ts"].as_string()).where(
            Projection.company_id == company_id,
            Projection.entity_id.in_([prefix + suffix for suffix in copies])))).all())
    if earliest := min((str(d)[:10] for d in dates if d), default=None):
        await _check_period_lock(session, company_id, {"ts": earliest})

    before = _choices_of(state, journal)
    async with fund_change_action(session, company_id, actor) as action:
        if journal:
            root = record_id.split(":fund:", 1)[0]
            taken = await session.scalar(select(func.count()).select_from(Projection).where(
                Projection.company_id == company_id,
                Projection.entity_id.startswith(f"{root}:fund:", autoescape=True)))
            copy_id = f"{root}:fund:{taken + 1}"
            await emit_event(
                session, company_id=company_id, entity_id=record_id, entity_type="journal_entry",
                event_type="acc.journal_entry.voided", data={**je_void_data(reason, state), "replaced_by": copy_id},
                actor_id=actor, location_id=None, source="fund_change", idempotency_key=f"{key}:void")
            lines = [{k: v for k, v in line.items() if k not in (*FUND_KEYS, "posted_from")} | chosen
                     for line, chosen in zip(copy_posted_lines(record_id, bare, reverse=False), keys)]
            data = {k: state[k] for k in ("memo", "ts", "je_type", "peer_origin", "peer_source", "reverses",
                                          "source", "origin") if state.get(k) is not None}
            await emit_event(
                session, company_id=company_id, entity_id=copy_id, entity_type="journal_entry",
                event_type="acc.journal_entry.created",
                data={**data, "status": "posted", "entries": lines, **grant.header},
                actor_id=actor, location_id=None, source="fund_change", idempotency_key=f"{key}:copy",
                fund_grant=grant, keep_gifts=True)
            result = {"copies": {record_id: copy_id}}
        else:
            from celerp_docs.routes import write_fund_choices

            await write_fund_choices(session, company_id, record_id, state, actor_id=actor,
                                     idempotency_key=f"{key}:choices", lines=change.lines,
                                     **({} if document is _ABSENT else {"document": document}))
            await session.refresh(row)
            relabel = document_relabel(row.state or {}, settings, str(settings.get("currency") or "USD"))
            batch = await auto_je.void_record_postings(
                session, company_id=company_id, user_id=actor, doc_id=record_id, trigger="fund_change",
                reason=reason, goods_movements=True, replaced_by=copies)
            reposted = await auto_je.repost_record_postings(
                session, company_id=company_id, user_id=actor, doc_id=record_id, batch=batch, relabel=relabel,
                trigger="fund_change", goods_movements=True)
            await doc_posting_dependents.move(session, company_id=company_id, user_id=actor, doc_id=record_id,
                                              relabel=relabel)
            result = {"copies": reposted}
        if not action.moves():
            raise _refused(422, "posting.fund.change_moves_nothing")
    after = (await session.get(Projection, {"company_id": company_id, "entity_id": result["copies"][record_id]})
             if journal else row)
    await emit_event(
        session, company_id=company_id, entity_id=record_id, entity_type="journal_entry" if journal else "doc",
        event_type="acc.journal_entry.fund_changed" if journal else "doc.fund_changed",
        data={"reason": reason, "from": before, "to": _choices_of(after.state or {}, journal),
              "authorized_by": action.authorized_by, "result": result},
        actor_id=actor, location_id=None, source="fund_change", idempotency_key=key,
        metadata_={"request": request_fingerprint(request)})
    return result
