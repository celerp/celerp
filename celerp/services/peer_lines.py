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

from celerp.accounting_roles import catalog_refusal

LINE_KEYS = ("origin", "fund_choice", "fund_id")
HEADER_KEYS = ("origin", "fund_choice", "fund_choice_source")
UNRESTRICTED = "unrestricted"  # the choice of no fund
# What a marked line's money is worked out from: a change to any of them clears the choices.
_LINE_MONEY_KEYS = ("item_id", "account", "quantity", "unit_price", "discount", "taxes")
# Where an outside record came from, each with its label (fund.origin.<origin>).
ORIGINS = frozenset({"file_import", "migration", "connected_company", "accountant", "client",
                     "shared_document", "accounting_system"})


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


async def require_fund_choices(session, company_id, record: Mapping, *, document: Any = _ABSENT,
                               every: Any = _ABSENT, lines: Mapping[str, Any]) -> FundChoiceGrant:
    """The grant of a local user's choice on ``record``: ``document`` its document choice,
    ``lines`` a choice per marked line (by line id) and ``every`` the choice of each marked
    line ``lines`` does not name. Each value is ``"unrestricted"`` or an active fund of the
    company; a line's ``fund_id`` follows its choice (none for Unrestricted)."""
    from celerp.services.journal_accounts import funds_by_id

    funds = await funds_by_id(session, company_id)

    def checked(value, missing: str, **params):
        if value == UNRESTRICTED:
            return value
        if value is None or value == "":
            raise _refused(422, missing, **params)
        fund = funds.get(value)
        if fund is None:
            raise _refused(422, "posting.fund.unknown", fund=str(value))
        if fund.get("archived"):
            raise _refused(422, "posting.fund.archived", fund=fund.get("name", value))
        return value

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
            value = checked(lines[line_id], "posting.fund.choice_missing", line=n)
        elif every is not _ABSENT and line.get("origin"):
            value = checked(every, "posting.fund.choice_missing", line=n)
        else:
            continue
        granted[line_id] = {"fund_choice": value, "fund_id": None if value == UNRESTRICTED else value}
    header = {}
    if document is not _ABSENT:
        header = {"fund_choice": checked(document, "posting.fund.document_choice_missing"),
                  "fund_choice_source": "user"}
    return FundChoiceGrant(lines=granted, header=header)


def chosen_lines(lines: list, grant: FundChoiceGrant) -> list:
    """``lines`` with the choices ``grant`` names applied (a fund of none drops ``fund_id``)."""
    out = []
    for line in lines:
        keys = grant.lines.get(line.get("line_id")) if isinstance(line, dict) else None
        if keys:
            line = {k: v for k, v in {**line, **keys}.items() if not (k == "fund_id" and v is None)}
        out.append(line)
    return out


def leg_funds(doc: Mapping) -> tuple[dict[int, str | None], str | None]:
    """The fund of each line of ``doc`` (by line number, 1-based) its entry posts to, and the
    fund of what no line stands behind. A line that came from outside goes where the company
    chose, its own choice before the document's; a local line goes to its ``fund_id``. With no
    choice, or the choice of Unrestricted, a line has no fund. The document's choice is the
    header's fund on an outside record; a local record's header has none."""
    def fund(choice):
        return None if choice in (None, "", UNRESTRICTED) else choice

    document = fund(doc.get("fund_choice")) if doc.get("origin") else None
    funds = {}
    for n, line in enumerate(doc.get("line_items") or [], 1):
        if not isinstance(line, dict):
            funds[n] = None
        elif line.get("origin"):
            funds[n] = fund(line.get("fund_choice")) if line.get("fund_choice") else document
        else:
            funds[n] = line.get("fund_id") or None
    return funds, document


def copied_header(state: Mapping) -> dict:
    """The fund keys of a record's header a copy of it carries."""
    return {key: state[key] for key in HEADER_KEYS if key in state}


def source_label(origin) -> dict:
    """Where a record came from, as a refusal param; an origin this version does not know
    reads as the neutral "another app"."""
    return catalog_refusal(f"fund.origin.{origin if origin in ORIGINS else 'unknown'}")


def _refused(status: int, message_key: str, /, **params) -> HTTPException:
    return HTTPException(status_code=status, detail=catalog_refusal(message_key, **params))


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
        for key in ("fund_choice", "fund_id"):
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


def inbound_keys(stored_lines, line_set, stored: Mapping, header: Mapping) -> list[str]:
    """The fund keys an arrival brings with it: on a line, a value other than the stored
    line's (matched by line id), and on the header a value other than the stored one. A
    value of none is no value."""
    kept = {li["line_id"]: li for li in stored_lines or [] if isinstance(li, dict) and li.get("line_id")}
    found = [key for key in HEADER_KEYS if header.get(key) is not None and header.get(key) != stored.get(key)]
    for line in line_set or ():
        if isinstance(line, dict):
            before = kept.get(line.get("line_id"), {})
            found += [key for key in LINE_KEYS if line.get(key) is not None and line.get(key) != before.get(key)]
    return list(dict.fromkeys(found))


def refuse_inbound(found: list[str]) -> None:
    """Where funds are on, the fund keys are this company's own: an arrival bringing one is
    refused naming it."""
    if found:
        raise _refused(422, "posting.fund.inbound_key", field=found[0])


def _measure(values: Mapping, key: str):
    """A money input as compared for a change: an empty or zero value is no value."""
    value = values.get(key)
    return None if value in (None, "", 0, [], {}) else value


def keep_fund_choices(stored: Mapping, line_set: list | None, written: Mapping) -> tuple[list | None, dict]:
    """Clear the record's fund choices when this write changes its money (LK5): a kept marked
    line's amounts (``_LINE_MONEY_KEYS``) or a header money input in ``written``. Every
    ``fund_choice`` and the ``fund_id`` of every marked line go, in the same write. Returns
    the line set to write (the stored lines cleared when the write carries none) and what
    was cleared, ``{"lines": [line numbers], "document": bool}``; ``(line_set, {})`` when
    nothing was."""
    from celerp_docs.doc_money import DOC_MONEY_INPUTS

    stored_lines = [li for li in stored.get("line_items") or [] if isinstance(li, dict)]
    held = any(li.get("fund_choice") for li in stored_lines) or bool(stored.get("fund_choice"))
    if not held:
        return line_set, {}
    before = {li.get("line_id"): li for li in stored_lines if li.get("origin")}
    changed = any(_measure(written, key) != _measure(stored, key)
                  for key in DOC_MONEY_INPUTS - {"line_items"} if key in written)
    for line in line_set or ():
        old = before.get(line.get("line_id")) if isinstance(line, dict) else None
        changed = changed or (old is not None and any(_measure(line, k) != _measure(old, k) for k in _LINE_MONEY_KEYS))
    if not changed:
        return line_set, {}
    lines = [dict(li) for li in stored_lines] if line_set is None else line_set
    cleared = []
    for n, line in enumerate(lines, 1):
        if isinstance(line, dict) and line.pop("fund_choice", None):
            cleared.append(n)
        if isinstance(line, dict) and line.get("origin"):
            line.pop("fund_id", None)
    return lines, {"lines": cleared, "document": bool(stored.get("fund_choice"))}


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


def mark_journal(data: dict, origin: str) -> None:
    """An arrival journal: the entry and every line carry ``origin`` (an inbound value stays as sent)."""
    data.setdefault("origin", origin)
    for line in data.get("entries") or []:
        if isinstance(line, dict):
            line.setdefault("origin", origin)
