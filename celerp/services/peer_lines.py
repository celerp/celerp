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
    ``posted_from``, so rule R lets it through without a new choice (check_outside_record)."""
    out = []
    for line in entries or []:
        if isinstance(line, dict):
            copied = {(_SWAPPED.get(k, k) if reverse else k): v for k, v in line.items()}
            out.append({**copied, **_posted_keys(line), "posted_from": entry_id})
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


def source_label(origin) -> dict:
    """Where a record came from, as a refusal param; an origin this version does not know
    reads as the neutral "another app"."""
    return catalog_refusal(f"fund.origin.{origin if origin in ORIGINS else 'unknown'}")


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
    from celerp.services.peer_documents import DOC_FUNDS
    from celerp_docs.doc_constants import TOTAL_POSTED_DOC_TYPES

    company = await session.get(Company, company_id)
    if company is None or not funds_enabled(dict(company.settings or {})):
        return {"count": 0, "held": 0, "documents": []}
    fixes = (await session.scalars(select(CompanyPeerAttention.fix).where(
        CompanyPeerAttention.company_id == company_id))).all()
    held = sum(1 for fix in fixes if (fix or {}).get("kind") in ("funds", DOC_FUNDS))
    rows = await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_type == "doc",
        Projection.state["doc_type"].as_string().in_((*TOTAL_POSTED_DOC_TYPES, "purchase_order")),
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
