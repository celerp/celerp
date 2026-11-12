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
  that call it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from celerp.accounting_roles import catalog_refusal

LINE_KEYS = ("origin", "fund_choice", "fund_id")
HEADER_KEYS = ("origin", "fund_choice", "fund_choice_source")
# Where an outside record came from, each with its label (fund.origin.<origin>).
ORIGINS = frozenset({"file_import", "migration", "connected_company", "accountant", "client",
                     "shared_document", "accounting_system"})


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


def copied_header(state: Mapping) -> dict:
    """The fund keys of a record's header a copy of it carries."""
    return {key: state[key] for key in HEADER_KEYS if key in state}


def source_label(origin) -> dict:
    """Where a record came from, as a refusal param; an origin this version does not know
    reads as the neutral "another app"."""
    return catalog_refusal(f"fund.origin.{origin if origin in ORIGINS else 'unknown'}")


def _refused(status: int, message_key: str, /, **params) -> HTTPException:
    return HTTPException(status_code=status, detail=catalog_refusal(message_key, **params))


_ABSENT = object()


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


def mark_journal(data: dict, origin: str) -> None:
    """An arrival journal: the entry and every line carry ``origin`` (an inbound value stays as sent)."""
    data.setdefault("origin", origin)
    for line in data.get("entries") or []:
        if isinstance(line, dict):
            line.setdefault("origin", origin)
