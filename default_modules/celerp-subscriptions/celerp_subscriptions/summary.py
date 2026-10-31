# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What unattended billing did, derived on read from the agreements' period records.

Nothing is copied: a billed period records when and whether it was billed unattended,
and its send records each attempt. A user's acknowledgment is a wall-clock time kept in
the company settings per user; the summary shows what was recorded after it. An
unattended mode whose authorizer no longer holds its permission is shown while it lasts.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.models.company import Company
from celerp.models.projections import Projection
from celerp.services.company_lock import locked_company
from celerp_subscriptions import billing
from ui.i18n import t

ACK_SETTING = "subscription_summary_ack"
KINDS = ("generated", "sent", "send_failed", "send_unconfirmed", "billing_lapsed", "sending_lapsed")
# A send recorded as going out with no outcome after it was cut off mid-send: it may or may not have arrived.
_SEND_KINDS = {"sent": "sent", "failed": "send_failed", "sending": "send_unconfirmed"}
_LAPSED = {"generation": "billing_lapsed", "sending": "sending_lapsed"}


async def acknowledge(session: AsyncSession, company_id, user_id) -> str:
    """Record that ``user_id`` has read the summary up to now."""
    company = await locked_company(session, company_id)
    at = billing.now_stamp()
    settings = dict(company.settings or {})
    settings[ACK_SETTING] = {**(settings.get(ACK_SETTING) or {}), str(user_id): at}
    company.settings = settings
    await session.flush()
    return at


async def _refs(session, company_id, doc_ids: set[str]) -> dict[str, str]:
    if not doc_ids:
        return {}
    rows = (await session.execute(select(Projection.entity_id, Projection.state).where(
        Projection.company_id == company_id, Projection.entity_id.in_(doc_ids)))).all()
    return {eid: (state or {}).get("ref_id") or eid for eid, state in rows}


async def report(session: AsyncSession, company_id, user_id, *, everything: bool = False) -> dict:
    """Counts by kind and the rows behind them, newest first."""
    company = await session.get(Company, company_id)
    settings = (company.settings or {}) if company else {}
    since = "" if everything else ((settings.get(ACK_SETTING) or {}).get(str(user_id)) or "")
    found = []
    for row in await billing.agreements(session, company_id):
        state = row.state
        for k, entry in (state.get("periods") or {}).items():
            base = {"entity_id": row.entity_id, "doc_id": entry.get("doc_id"), "period_index": int(k)}
            if entry.get("state") == billing.BILLED and entry.get("auto") and (entry.get("at") or "") > since:
                found.append({**base, "kind": "generated", "at": entry["at"]})
            send = entry.get("send") or {}
            if send.get("state") in _SEND_KINDS and (send.get("at") or "") > since:
                kind = _SEND_KINDS[send["state"]]
                found.append({**base, "kind": kind, "at": send["at"], "error": send.get("error"),
                              "retry_url": f"/subscriptions/{row.entity_id}/periods/{k}/send" if kind != "sent" else None})
        if state.get("status") != "active":
            continue
        for mode in billing.lapsed_modes(state):
            if await billing.authorizer(session, company_id, settings, state, mode) is None:
                found.append({"entity_id": row.entity_id, "doc_id": None, "period_index": None,
                              "kind": _LAPSED[mode], "at": None})
    refs = await _refs(session, company_id, {r["doc_id"] for r in found if r["doc_id"]})
    rows = []
    for r in found:
        params = {"ref": refs.get(r["doc_id"], r["doc_id"]), "error": r.pop("error", None) or ""}
        rows.append({"retry_url": None, **r, "ref_id": params["ref"] if r["doc_id"] else None,
                     "message": t(f"subscriptions.summary_{r['kind']}", **params)})
    rows.sort(key=lambda r: r["at"] or "", reverse=True)
    counts = {kind: sum(1 for r in rows if r["kind"] == kind) for kind in KINDS}
    return {"counts": counts, "rows": rows}
