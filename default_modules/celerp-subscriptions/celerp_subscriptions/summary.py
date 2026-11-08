# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What unattended billing did, derived on read from the agreements' period records.

Nothing is copied: a billed period records when and whether it was billed unattended,
and its send records each attempt. The summary covers SUMMARY_WINDOW back from now. A user's
acknowledgment is the keys of the rows the page they marked read showed, kept in the company
settings per user; the summary hides those rows and nothing else, and a key leaves storage with
its row. An unattended mode whose authorizer no longer holds its permission, an email waiting
to go out on its own, and a billing failure (one current fact on its agreement, which run_due
writes and drops) are shown while they last, whatever their age; an email that will not go on
its own is a row like any other. A failed send keeps its reason as a catalog key and a failed
bill the keyed refusal it met, each worded in the reader's language when read.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company, User
from celerp.models.projections import Projection
from celerp.services.business_time import company_today
from celerp.services.company_lock import locked_company
from celerp_subscriptions import billing
from ui.i18n import refusal_text, t

ACK_SETTING = "subscription_summary_ack"
# How far back the summary reads: a row whose time is older leaves it, and its key leaves
# storage on the next Mark as read, so what one user stores stays bounded.
SUMMARY_WINDOW = timedelta(days=90)
KINDS = ("generated", "generation_failed", "sent", "send_failed", "send_sending", "send_unconfirmed",
         "send_pending", "billing_lapsed", "sending_lapsed")
# Each billing.send_status and its summary kind. An email that waits to go out on its own is
# shown until it goes; every other row is shown until the page showing it is marked read. A row's
# key holds the time its reading began (the since of billing.send_state), so a send that turns
# cut off by age is a new row and shows again.
_SEND_KINDS = {"sent": "sent", "failed": "send_failed", "sending": "send_sending",
               "unconfirmed": "send_unconfirmed", "waiting": "send_pending", "not_sent": "send_pending"}
# The send kinds someone may need to act on: every one but a sent email.
TO_CHECK = tuple(dict.fromkeys(kind for kind in _SEND_KINDS.values() if kind != "sent"))
_LAPSED = {"generation": "billing_lapsed", "sending": "sending_lapsed"}
# The kinds someone may need to act on (counted "to check"), and those that only say what ran.
NEEDS_YOU = (*TO_CHECK, "generation_failed", *_LAPSED.values())
INFORMATIONAL = ("generated", "sent")


def _key(row: dict) -> str:
    return "|".join(str(row[f]) for f in ("kind", "entity_id", "period_index", "at"))


def _read(settings: dict, user_id) -> set[str]:
    """The keys of the rows ``user_id`` marked read."""
    read = (settings.get(ACK_SETTING) or {}).get(str(user_id))
    return {k for k in read if isinstance(k, str)} if isinstance(read, list) else set()


async def acknowledge(session: AsyncSession, company_id, user_id, keys: list[str]) -> list[str]:
    """Record that ``user_id`` has read the rows whose keys the page they read showed (each row
    of report carries its ``key``). Only keys of rows there now are kept, with those already
    read: a row the page never showed is never marked read, and a key whose row is gone drops."""
    company = await locked_company(session, company_id)
    settings = dict(company.settings or {})
    current = {r["key"] for r in await _rows(session, company_id, settings) if r["key"]}
    read = sorted(current & (set(keys) | _read(settings, user_id)))
    settings[ACK_SETTING] = {**(settings.get(ACK_SETTING) or {}), str(user_id): read}
    company.settings = settings
    await session.flush()
    return read


async def _states(session, company_id, entity_ids: set[str]) -> dict[str, dict]:
    """Each entity's state as stored now (read past the session's identity map)."""
    if not entity_ids:
        return {}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id.in_(entity_ids)))).all()
    return {eid: state or {} for eid, state in rows}


def _send_kind(status: str | None, send: dict) -> str | None:
    """The summary kind of a period's email as billing.send_status reads it. A sent email that
    someone sent (Send, Retry or Send again, or the document's own page: a record that alone
    does not read sent) was sent by hand."""
    if status == "sent" and (send.get("by") or send.get("via")
                             or billing.send_state(send, billing.now())[0] != "sent"):
        return "sent_by_hand"
    return _SEND_KINDS.get(status)


def _message(kind: str, ref: str | None, error_key: str | None) -> str:
    return t(f"subscriptions.summary_{kind}", ref=ref or "", error=t(error_key) if error_key else "")


async def send_answer(session: AsyncSession, company_id, entity_id: str, k: int) -> dict:
    """What a send from the page did, read after it from the agreement and its document as
    stored: the period's email as billing.send_status reads it and the summary's sentence for it."""
    state = (await _states(session, company_id, {entity_id})).get(entity_id) or {}
    entry = billing.periods_of(state).get(str(k)) or {}
    doc_id = entry.get("doc_id")
    doc = (await _states(session, company_id, {doc_id})).get(doc_id) if doc_id else None
    send = entry.get("send") or {}
    status, _since = billing.send_status(state, k, await company_today(session, company_id), doc)
    kind = _send_kind(status, send)
    return {"ok": True, "period_index": k, "send": status,
            "message": _message(kind, (doc or {}).get("ref_id") or doc_id, send.get("error_key")) if kind else None}


async def _member_name(session, user_id) -> str:
    try:
        name = await session.scalar(select(User.name).where(User.id == uuid.UUID(str(user_id))))
    except ValueError:
        name = None
    return name or t("subscriptions.runs_former_member")


async def modes_answer(session: AsyncSession, company_id, entity_id: str) -> dict:
    """What runs on its own for the agreement now, read from it as stored after a status
    change: each mode in force and the member whose authorization it runs under, or whose
    authorization has lapsed so it does not run; that it is billed by hand when none is."""
    state = (await _states(session, company_id, {entity_id})).get(entity_id) or {}
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    runs, lapsed, sentences = [], [], []
    for mode in billing.modes_in_force(state):
        name = await _member_name(session, ((state.get("consents") or {}).get(mode) or {}).get("by"))
        if await billing.authorizer(session, company_id, settings, state, mode) is None:
            lapsed.append(mode)
            sentences.append(t(f"subscriptions.runs_{mode}_lapsed", name=name))
        else:
            runs.append(mode)
            sentences.append(t(f"subscriptions.runs_{mode}", name=name))
    return {"runs": runs, "lapsed": lapsed, "message": " ".join(sentences) or t("subscriptions.runs_by_hand")}


async def _rows(session: AsyncSession, company_id, settings: dict) -> list[dict]:
    """Every row the summary can show, each with its ``key`` (None for one shown while it lasts:
    an email waiting to go out on its own, a mode whose authorization lapsed, a billing failure).
    A keyed row whose time is older than SUMMARY_WINDOW is left out (one whose time cannot be
    read stays)."""
    replayable = await billing.replayable_operations(session, company_id)
    today = await company_today(session, company_id)
    # One whose stored schedule can't be read reports nothing here; the list and its page name it.
    rows = await billing.agreements(session, company_id)
    agreement_rows = [row for row in rows if billing.unscheduled(row.state) is None]
    docs = await _states(session, company_id, {e["doc_id"] for row in agreement_rows
                                               for e in billing.periods_of(row.state).values() if e.get("doc_id")})
    found = []
    for row in agreement_rows:
        state = row.state
        for k, entry in (state.get("periods") or {}).items():
            base = {"entity_id": row.entity_id, "doc_id": entry.get("doc_id"), "period_index": int(k)}
            if entry.get("state") == billing.BILLED and entry.get("auto") and entry.get("at"):
                found.append({**base, "kind": "generated", "at": entry["at"]})
            send = entry.get("send") or {}
            status, began = billing.send_status(state, k, today, docs.get(entry.get("doc_id")))
            # An email someone sent by hand is theirs, not this summary's of what ran on its own.
            kind = _send_kind(status, send)
            if kind and kind != "sent_by_hand" and (status == "waiting" or began):
                found.append({**base, "kind": kind, "send": status, "at": began, "lasts": status == "waiting",
                              "error_key": send.get("error_key"), "attempt": int(send.get("n") or 0),
                              "again": billing.sends_again(status, send, replayable),
                              "retry_url": (f"/subscriptions/{row.entity_id}/periods/{k}/send"
                                            if status != "sent" else None)})
        if failure := billing.failure_of(state):
            found.append({"entity_id": row.entity_id, "doc_id": row.entity_id,
                          "period_index": failure.get("period_index"), "kind": "generation_failed",
                          "at": failure.get("at"), "reason": failure.get("reason"), "lasts": True})
        for mode in billing.modes_in_force(state):
            if await billing.authorizer(session, company_id, settings, state, mode) is None:
                found.append({"entity_id": row.entity_id, "doc_id": None, "period_index": None,
                              "kind": _LAPSED[mode], "at": None, "lasts": True})
    named = {**docs, **await _states(session, company_id, {r["doc_id"] for r in found
                                                            if r["doc_id"] and r["doc_id"] not in docs})}
    since = billing.now() - SUMMARY_WINDOW
    out = []
    for r in found:
        lasts = r.pop("lasts", False)
        if not lasts and (at := billing.stamp_time(r["at"])) is not None and at < since:
            continue
        ref = (named.get(r["doc_id"]) or {}).get("ref_id") or r["doc_id"]
        reason = refusal_text(r.pop("reason", None))
        message = (t("subscriptions.summary_generation_failed_because", ref=ref, error=reason) if reason
                   else _message(r["kind"], ref, r.get("error_key")))
        out.append({"retry_url": None, "again": False, **r, "ref_id": ref if r["doc_id"] else None,
                    "message": message, "key": None if lasts else _key(r)})
    return out


async def report(session: AsyncSession, company_id, user_id, *, everything: bool = False) -> dict:
    """Counts by kind and the rows behind them, newest first, each with the ``key`` Mark as read
    posts for it: every row but those this user marked read (``everything``: all of them)."""
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    read = set() if everything else _read(settings, user_id)
    rows = [r for r in await _rows(session, company_id, settings) if r["key"] not in read]
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    counts = {kind: sum(1 for r in rows if r["kind"] == kind) for kind in KINDS}
    return {"counts": counts, "rows": rows}
