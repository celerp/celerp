# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Maintenance, warranty and insurance on an asset.

A reminder (``fa:reminder:<asset>:<uuid>``) is due on a date and tells its responsible
person once when it comes within its lead time and again on the due date. It is marked
done (the next one scheduled when it recurs), postponed with a reason, edited or removed,
and every change is kept in its history, newest first; the latest change is undone by
restoring what it changed.

The maintenance log holds the work done on an asset: entries recorded here, entries a
reminder marked done, and repairs registered from a bill (operations.register), whose
date and amount come from the bill and are shown only to roles that see money.
"""
from __future__ import annotations

import calendar
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from celerp.db import get_session
from celerp.services.auth import get_current_company_id, get_current_role, get_current_user
from celerp.services.company_lock import lock_projections
from celerp.services.money import books_currency
from celerp.services.permissions import get_current_company_settings, require_permission, role_has_permission

from celerp_fixed_assets import files, setup
from celerp_fixed_assets.custody import _today
from celerp_fixed_assets.moves import _GONE, _locked_assets, member_name
from celerp_fixed_assets.store import _emit, _entities, _state

router = APIRouter(prefix="/fixed-assets/api", tags=["fixed-assets"], dependencies=[Depends(get_current_user)])
_VIEW = [require_permission("view_fixed_assets")]
_MANAGE = [require_permission("manage_fixed_assets")]

REMINDER = "fa_reminder"
MAINT = "fa_maint"
REMINDERS_URL = "/fixed-assets/reminders"
NOTICE_TITLE = "Asset reminders"
NOTICE_KEY = "fixed_assets.notice.reminders"
# Each kind of reminder and the field that says what it is about.
KINDS = {"maintenance": "work", "warranty": "provider", "insurance": "insurer"}
_DETAIL_REFUSAL = {"work": "Describe the maintenance work.", "provider": "Name the warranty provider.",
                   "insurer": "Name the insurer."}
EDITABLE = ("due", "every_months", "lead_days", "responsible_user_id", "policy", "insured_value", *KINDS.values())
LEAD_DAYS = 30
MAINT_EDITABLE = ("date", "work", "by")


def add_months(day: str, months: int) -> str:
    d = date.fromisoformat(day)
    index = d.year * 12 + d.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1])).isoformat()


def _day(value, refusal: str) -> str:
    try:
        return date.fromisoformat(str(value or "")).isoformat()
    except ValueError:
        raise setup.unprocessable(refusal) from None


def _text(value) -> str | None:
    return str(value).strip() or None if value is not None else None


def _whole(value, low: int, high: int, refusal: str) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise setup.unprocessable(refusal) from None
    if not low <= number <= high:
        raise setup.unprocessable(refusal)
    return number


async def _field(session, company_id, field: str, value, kind: str):
    """One reminder field, checked and normalized; a refusal names what to fix."""
    if field == "due":
        return _day(value, "Enter the due date.")
    if field == "every_months":
        if value in (None, ""):
            return None
        return _whole(value, 1, 120, "Repeat every whole number of months, from 1 to 120, or leave it empty.")
    if field == "lead_days":
        if value in (None, ""):
            return LEAD_DAYS
        return _whole(value, 0, 365, "Tell this many days ahead: a whole number between 0 and 365.")
    if field == "responsible_user_id":
        if value in (None, ""):
            return None
        if not await member_name(session, company_id, value):
            raise setup.unprocessable("The responsible person is not a member of this company. Choose a member.")
        return str(uuid.UUID(str(value)))
    if field == "insured_value":
        if value in (None, ""):
            return None
        from celerp.services.money import round_money
        from celerp_fixed_assets.disposal import _number

        try:
            amount = _number(value, "The insured value")
        except HTTPException:
            raise setup.unprocessable("Enter the insured value as an amount, such as 25000.") from None
        if amount < 0:
            raise setup.unprocessable("Enter the insured value as an amount, such as 25000.")
        return str(round_money(amount, books_currency(await setup.company_settings(session, company_id))))
    text = _text(value)
    if field == KINDS[kind] and not text:
        raise setup.unprocessable(_DETAIL_REFUSAL[field])
    return text


def detail(reminder: dict) -> str:
    """What the reminder is about: the work, the warranty provider or the insurer."""
    return reminder.get(KINDS[reminder["kind"]]) or ""


def _history(action: str, user_id, before: dict | None = None, after: dict | None = None, **extra) -> dict:
    return {"id": str(uuid.uuid4()), "action": action, "at": datetime.now(timezone.utc).isoformat(), "user_id": str(user_id),
            "before": before or {}, "after": after or {}, **extra}


def reminder_out(reminder_id: str, state: dict) -> dict:
    return {"id": reminder_id, **{k: v for k, v in state.items() if k != "told"}}


async def _live_asset(session, company_id, asset_id: str) -> dict:
    ((_, state),) = await _locked_assets(session, company_id, [asset_id])
    if not state.get("number"):
        raise HTTPException(status_code=404, detail="No such asset.")
    if state.get("status") in _GONE:
        raise setup.unprocessable(f"{state['number']} is no longer on the books.")
    return state


async def _new_reminder(session, company_id, user_id, asset_id: str, body: dict) -> str:
    kind = body.get("kind")
    if kind not in KINDS:
        raise setup.unprocessable("Choose maintenance, warranty or insurance.")
    state = {"asset_id": asset_id, "kind": kind, "status": "open"}
    for field in (KINDS[kind], "due", "every_months", "lead_days", "responsible_user_id",
                  *(("policy", "insured_value") if kind == "insurance" else ())):
        state[field] = await _field(session, company_id, field, body.get(field), kind)
    state["history"] = [_history("created", user_id)]
    reminder_id = f"fa:reminder:{asset_id.removeprefix('fa:asset:')}:{uuid.uuid4()}"
    await _emit(session, company_id, user_id, reminder_id, REMINDER, "fa.reminder.created", state)
    return reminder_id


async def add_reminder(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    await _live_asset(session, company_id, asset_id)
    reminder_id = await _new_reminder(session, company_id, user_id, asset_id, body)
    await session.commit()
    return reminder_out(reminder_id, await _state(session, company_id, reminder_id))


async def _locked(session, company_id, entity_id: str, entity_type: str) -> dict:
    row = (await lock_projections(session, company_id, [entity_id])).get(entity_id)
    if row is None or row.entity_type != entity_type:
        raise HTTPException(status_code=404, detail="No such reminder." if entity_type == REMINDER
                            else "No such maintenance entry.")
    return row.state


async def _change(session, company_id, user_id, reminder_id: str, changes: dict, row: dict) -> dict:
    await _emit(session, company_id, user_id, reminder_id, REMINDER, "fa.reminder.changed",
                {"changes": changes, "history": row})
    await session.commit()
    return reminder_out(reminder_id, await _state(session, company_id, reminder_id))


def _open(reminder: dict) -> None:
    if reminder["status"] == "done":
        raise setup.unprocessable("This reminder is already done.")
    if reminder["status"] == "removed":
        raise setup.unprocessable("This reminder was removed. Undo the removal first.")


async def _record_maintenance(session, company_id, user_id, asset_id: str, data: dict) -> str:
    maint_id = f"fa:maint:{asset_id.removeprefix('fa:asset:')}:{uuid.uuid4()}"
    await _emit(session, company_id, user_id, maint_id, MAINT, "fa.maint.recorded", {
        "asset_id": asset_id, "source": None, "amount": None, "status": "active", "files": [], **data})
    return maint_id


async def mark_done(session, company_id, user_id, reminder_id: str, body: dict) -> dict:
    """The item was done on ``date``: the next one is scheduled when it recurs, else it is
    closed; done maintenance goes in the asset's maintenance log."""
    reminder = await _locked(session, company_id, reminder_id, REMINDER)
    _open(reminder)
    day = _day(body.get("date"), "Enter the date the work was done.")
    every = reminder.get("every_months")
    after = {"due": add_months(day, every)} if every else {"status": "done"}
    before = {k: reminder.get(k) for k in after}
    extra = {}
    if reminder["kind"] == "maintenance":
        extra["maint_id"] = await _record_maintenance(session, company_id, user_id, reminder["asset_id"], {
            "date": day, "work": reminder.get("work"), "by": await member_name(session, company_id, user_id),
            "next_due": after.get("due"), "reminder_id": reminder_id})
    return await _change(session, company_id, user_id, reminder_id, after,
                         _history("done", user_id, before, after, date=day, **extra))


async def postpone(session, company_id, user_id, reminder_id: str, body: dict) -> dict:
    reminder = await _locked(session, company_id, reminder_id, REMINDER)
    _open(reminder)
    to = _day(body.get("to"), "Enter the date to postpone to.")
    reason = _text(body.get("reason"))
    if not reason:
        raise setup.unprocessable("Say why it is postponed.")
    if to <= reminder["due"]:
        raise setup.unprocessable(f"Postpone to a date after {reminder['due']}.")
    return await _change(session, company_id, user_id, reminder_id, {"due": to},
                         _history("postponed", user_id, {"due": reminder["due"]}, {"due": to}, reason=reason))


async def edit(session, company_id, user_id, reminder_id: str, changes: dict) -> dict:
    reminder = await _locked(session, company_id, reminder_id, REMINDER)
    if reminder["status"] == "removed":
        raise setup.unprocessable("This reminder was removed. Undo the removal first.")
    if "kind" in changes:
        raise setup.unprocessable("The kind of a reminder cannot be changed. Remove it and add a new one.")
    allowed = {KINDS[reminder["kind"]], "due", "every_months", "lead_days", "responsible_user_id",
               *(("policy", "insured_value") if reminder["kind"] == "insurance" else ())}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise setup.unprocessable(f"{unknown[0]} cannot be edited on this reminder.")
    after = {f: await _field(session, company_id, f, v, reminder["kind"]) for f, v in changes.items()}
    after = {k: v for k, v in after.items() if reminder.get(k) != v}
    if not after:
        return reminder_out(reminder_id, reminder)
    return await _change(session, company_id, user_id, reminder_id, after,
                         _history("edited", user_id, {k: reminder.get(k) for k in after}, after))


async def remove(session, company_id, user_id, reminder_id: str) -> dict:
    reminder = await _locked(session, company_id, reminder_id, REMINDER)
    if reminder["status"] == "removed":
        raise setup.unprocessable("This reminder is already removed.")
    return await _change(session, company_id, user_id, reminder_id, {"status": "removed"},
                         _history("removed", user_id, {"status": reminder["status"]}, {"status": "removed"}))


async def undo(session, company_id, user_id, reminder_id: str) -> dict:
    """Undo the latest change still standing: what it changed goes back, and a done
    maintenance item's log entry is withdrawn with it. Adding a reminder is undone by
    removing it."""
    reminder = await _locked(session, company_id, reminder_id, REMINDER)
    index = next((i for i, h in enumerate(reminder.get("history") or [])
                  if h["action"] not in ("created", "undone") and not h.get("undone")), None)
    if index is None:
        raise setup.unprocessable("Nothing to undo on this reminder.")
    latest = reminder["history"][index]
    if latest.get("maint_id"):
        await _emit(session, company_id, user_id, latest["maint_id"], MAINT, "fa.maint.status", {"status": "undone"})
    await _emit(session, company_id, user_id, reminder_id, REMINDER, "fa.reminder.changed", {
        "changes": latest["before"], "undid": latest["id"],
        "history": _history("undone", user_id, latest["after"], latest["before"], of=latest["action"])})
    await session.commit()
    return reminder_out(reminder_id, await _state(session, company_id, reminder_id))


# ── Maintenance log ──────────────────────────────────────────────────────────


async def add_entry(session, company_id, user_id, asset_id: str, body: dict) -> dict:
    """Work done on an asset; a next due date adds a maintenance reminder for it."""
    await _live_asset(session, company_id, asset_id)
    day = _day(body.get("date"), "Enter the date the work was done.")
    work = _text(body.get("work"))
    if not work:
        raise setup.unprocessable("Describe the maintenance work.")
    data = {"date": day, "work": work, "by": _text(body.get("by")), "next_due": None, "reminder_id": None}
    if body.get("next_due") not in (None, ""):
        data["reminder_id"] = await _new_reminder(session, company_id, user_id, asset_id, {
            "kind": "maintenance", "work": work, "due": body.get("next_due"), "every_months": body.get("every_months")})
        data["next_due"] = (await _state(session, company_id, data["reminder_id"]))["due"]
    maint_id = await _record_maintenance(session, company_id, user_id, asset_id, data)
    await session.commit()
    return {"id": maint_id, **await _state(session, company_id, maint_id)}


async def edit_entry(session, company_id, user_id, maint_id: str, changes: dict) -> dict:
    entry = await _locked(session, company_id, maint_id, MAINT)
    from_bill = bool(entry.get("source"))
    if from_bill and {"date", "amount"} & set(changes):
        raise setup.unprocessable(f"The {'date' if 'date' in changes else 'amount'} comes from the bill. Correct the "
                                  "bill to change it.")
    unknown = sorted(set(changes) - set(MAINT_EDITABLE))
    if unknown:
        raise setup.unprocessable(f"{unknown[0]} cannot be edited on a maintenance entry.")
    after = {k: _day(v, "Enter the date the work was done.") if k == "date" else _text(v) for k, v in changes.items()}
    if "work" in after and not after["work"]:
        raise setup.unprocessable("Describe the maintenance work.")
    after = {k: v for k, v in after.items() if entry.get(k) != v}
    if after:
        await _emit(session, company_id, user_id, maint_id, MAINT, "fa.maint.updated", {"changes": after})
        await session.commit()
    return {"id": maint_id, **await _state(session, company_id, maint_id)}


async def set_entry_status(session, company_id, user_id, maint_id: str, status: str) -> dict:
    entry = await _locked(session, company_id, maint_id, MAINT)
    if entry.get("source"):
        raise setup.unprocessable("This maintenance came from a bill. To remove it, undo its registration.")
    if entry["status"] == "undone":
        raise HTTPException(status_code=404, detail="No such maintenance entry.")
    if entry["status"] != status:
        await _emit(session, company_id, user_id, maint_id, MAINT, "fa.maint.status", {"status": status})
        await session.commit()
    return {"id": maint_id, **await _state(session, company_id, maint_id)}


async def attach_to_entry(session, company_id, user_id, maint_id: str, file: UploadFile) -> dict:
    """Store a receipt or report on the asset, tagged maintenance, and list it on the entry."""
    entry = await _state(session, company_id, maint_id)
    if entry is None or entry.get("status") == "undone":
        raise HTTPException(status_code=404, detail="No such maintenance entry.")

    async def note(_state_of_asset: dict, meta: dict) -> None:
        locked = await _locked(session, company_id, maint_id, MAINT)
        await _emit(session, company_id, user_id, maint_id, MAINT, "fa.maint.updated",
                    {"changes": {"files": [*(locked.get("files") or []), meta["id"]]}})

    await files.store_file(session, company_id, user_id, entry["asset_id"], file, "maintenance", also=note)
    return {"id": maint_id, **await _state(session, company_id, maint_id)}


def entry_out(maint_id: str, entry: dict, asset: dict | None, *, currency: str | None) -> dict:
    """A log entry as shown; its amount (from the bill) only with a ``currency``, which is
    given to roles that see money."""
    from celerp.services.money import round_money

    stored = {f["id"]: f for f in (asset or {}).get("files") or []}
    amount = entry.get("amount")
    return {"id": maint_id, "date": entry.get("date"), "work": entry.get("work"), "by": entry.get("by"),
            "next_due": entry.get("next_due"), "reminder_id": entry.get("reminder_id"),
            "amount": str(round_money(amount, currency)) if currency and amount is not None else None,
            "bill_ref": entry.get("bill_ref"),
            "source": entry.get("source"), "status": entry["status"],
            "files": [stored[f] for f in entry.get("files") or [] if f in stored]}


async def of_asset(session, company_id, asset_id: str, *, money: bool) -> dict:
    """An asset's reminders, open ones soonest first, and its maintenance log, newest first."""
    asset = await _state(session, company_id, asset_id)
    if asset is None or not asset.get("number"):
        raise HTTPException(status_code=404, detail="No such asset.")
    currency = books_currency(await setup.company_settings(session, company_id)) if money else None
    reminders = [reminder_out(r, s) for r, s in await _entities(session, company_id, REMINDER)
                 if s["asset_id"] == asset_id]
    log = [entry_out(m, s, asset, currency=currency) for m, s in await _entities(session, company_id, MAINT)
           if s["asset_id"] == asset_id and s["status"] != "undone"]
    return {"reminders": sorted(reminders, key=lambda r: (r["status"] != "open", r["due"])),
            "maintenance": sorted(log, key=lambda m: m["date"] or "", reverse=True)}


async def due_items(session, company_id, today: str) -> list[dict]:
    """Open reminders within their lead time or past due, on assets still on the books,
    soonest first."""
    assets = dict(await _entities(session, company_id, "fa_asset"))
    out = []
    for reminder_id, r in await _entities(session, company_id, REMINDER):
        asset = assets.get(r["asset_id"]) or {}
        if r["status"] != "open" or asset.get("status") in _GONE or not asset.get("number"):
            continue
        if today < (date.fromisoformat(r["due"]) - timedelta(days=int(r["lead_days"]))).isoformat():
            continue
        out.append({"id": reminder_id, "asset_id": r["asset_id"], "number": asset["number"], "name": asset.get("name"),
                    "kind": r["kind"], "detail": detail(r), "due": r["due"], "overdue": r["due"] < today,
                    "responsible_user_id": r.get("responsible_user_id") or asset.get("responsible_user_id"),
                    "told": r.get("told") or []})
    return sorted(out, key=lambda r: (r["due"], r["number"]))


async def _managers(session, company_id) -> list[str]:
    """The active members whose role may manage fixed assets."""
    from sqlalchemy import select

    from celerp.models.accounting import UserCompany
    from celerp.services.auth import normalize_role

    settings = await setup.company_settings(session, company_id)
    rows = (await session.execute(select(UserCompany.user_id, UserCompany.role).where(
        UserCompany.company_id == company_id, UserCompany.is_active.is_(True)))).all()
    return [str(uid) for uid, role in rows if role_has_permission(settings, normalize_role(role), "manage_fixed_assets")]


async def tell_reminders(session, company_id, today: date) -> None:
    """Tell each responsible person, in one notice, the items that came within their lead
    time or fell due since they were last told; an item no one is responsible for is told
    to everyone who manages fixed assets. Each item is told once per stage. The caller commits."""
    from celerp.notifications.service import notify_once

    day = today.isoformat()
    grouped: dict[str, list[str]] = {}
    managers = None
    for item in await due_items(session, company_id, day):
        stage = f"{'due' if day >= item['due'] else 'soon'}:{item['due']}"
        if stage in item["told"]:
            continue
        await _emit(session, company_id, None, item["id"], REMINDER, "fa.reminder.told", {"stage": stage},
                    key=f"{item['id']}:told:{stage}")
        if item["responsible_user_id"]:
            to = [item["responsible_user_id"]]
        else:
            managers = managers if managers is not None else await _managers(session, company_id)
            to = managers
        line = f"{item['number']} {item['name'] or ''}: {item['detail']}, due {item['due']}"
        for user_id in to:
            grouped.setdefault(user_id, []).append(line)
    for user_id, lines in grouped.items():
        items = "\n".join(lines)
        await notify_once(session, company_id, "fixed_assets", NOTICE_TITLE,
                          f"These asset reminders are due:\n{items}", user_id=uuid.UUID(user_id),
                          action_url=REMINDERS_URL,
                          i18n={"title": f"{NOTICE_KEY}.title", "body": f"{NOTICE_KEY}.body", "params": {"items": items}})


# ── Routes ───────────────────────────────────────────────────────────────────


@router.get("/assets/{asset_id}/upkeep", dependencies=_VIEW)
async def get_upkeep(asset_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                     company_settings: dict = Depends(get_current_company_settings),
                     session: AsyncSession = Depends(get_session)) -> dict:
    money = role_has_permission(company_settings, role, "view_financial_reports")
    return await of_asset(session, company_id, asset_id, money=money)


@router.post("/assets/{asset_id}/reminders", dependencies=_MANAGE)
async def create_reminder(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                          user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await add_reminder(session, company_id, user.id, asset_id, body)


@router.get("/reminders", dependencies=_VIEW)
async def list_due(company_id=Depends(get_current_company_id), session: AsyncSession = Depends(get_session)) -> dict:
    today = await _today(session, company_id)
    return {"items": [{k: v for k, v in r.items() if k != "told"} for r in await due_items(session, company_id, today)]}


async def _one(session, company_id, entity_id: str, entity_type: str) -> dict:
    state = await _state(session, company_id, entity_id)
    if state is None or not entity_id.startswith(f"fa:{entity_type.removeprefix('fa_')}:") \
            or state.get("status") == "undone":
        raise HTTPException(status_code=404, detail="No such reminder." if entity_type == REMINDER
                            else "No such maintenance entry.")
    return state


@router.get("/reminders/{reminder_id}", dependencies=_VIEW)
async def get_reminder(reminder_id: str, company_id=Depends(get_current_company_id),
                       session: AsyncSession = Depends(get_session)) -> dict:
    return reminder_out(reminder_id, await _one(session, company_id, reminder_id, REMINDER))


@router.post("/reminders/{reminder_id}/done", dependencies=_MANAGE)
async def reminder_done(reminder_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await mark_done(session, company_id, user.id, reminder_id, body)


@router.post("/reminders/{reminder_id}/postpone", dependencies=_MANAGE)
async def reminder_postpone(reminder_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                            user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await postpone(session, company_id, user.id, reminder_id, body)


@router.patch("/reminders/{reminder_id}", dependencies=_MANAGE)
async def reminder_edit(reminder_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                        user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await edit(session, company_id, user.id, reminder_id, dict(body.get("changes") or {}))


@router.post("/reminders/{reminder_id}/remove", dependencies=_MANAGE)
async def reminder_remove(reminder_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)) -> dict:
    return await remove(session, company_id, user.id, reminder_id)


@router.post("/reminders/{reminder_id}/undo", dependencies=_MANAGE)
async def reminder_undo(reminder_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> dict:
    return await undo(session, company_id, user.id, reminder_id)


@router.post("/assets/{asset_id}/maintenance", dependencies=_MANAGE)
async def create_entry(asset_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                       user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await add_entry(session, company_id, user.id, asset_id, body)


@router.get("/maintenance/{maint_id}", dependencies=_VIEW)
async def get_entry(maint_id: str, company_id=Depends(get_current_company_id), role: str = Depends(get_current_role),
                    company_settings: dict = Depends(get_current_company_settings),
                    session: AsyncSession = Depends(get_session)) -> dict:
    entry = await _one(session, company_id, maint_id, MAINT)
    money = role_has_permission(company_settings, role, "view_financial_reports")
    return {"asset_id": entry["asset_id"], **entry_out(
        maint_id, entry, await _state(session, company_id, entry["asset_id"]),
        currency=books_currency(company_settings) if money else None)}


@router.patch("/maintenance/{maint_id}", dependencies=_MANAGE)
async def entry_edit(maint_id: str, body: dict = Body(...), company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await edit_entry(session, company_id, user.id, maint_id, dict(body.get("changes") or {}))


@router.post("/maintenance/{maint_id}/remove", dependencies=_MANAGE)
async def entry_remove(maint_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)) -> dict:
    return await set_entry_status(session, company_id, user.id, maint_id, "removed")


@router.post("/maintenance/{maint_id}/restore", dependencies=_MANAGE)
async def entry_restore(maint_id: str, company_id=Depends(get_current_company_id), user=Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)) -> dict:
    return await set_entry_status(session, company_id, user.id, maint_id, "active")


@router.post("/maintenance/{maint_id}/files", dependencies=_MANAGE)
async def entry_file(maint_id: str, file: UploadFile, company_id=Depends(get_current_company_id),
                     user=Depends(get_current_user), session: AsyncSession = Depends(get_session)) -> dict:
    return await attach_to_entry(session, company_id, user.id, maint_id, file)
