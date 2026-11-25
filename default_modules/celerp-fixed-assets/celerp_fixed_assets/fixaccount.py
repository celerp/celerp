# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Fix posting account: give a class a new account when the chart no longer accepts one
it posts to, and the run holds its assets until it does.

The depreciation expense account is a profit and loss account: what was posted there
stays in its periods, so it changes for future postings only. The asset and accumulated
depreciation accounts are balance sheet accounts: the class's balances there move to the
new account in one journal, so nothing is stranded on an account no longer in use. The
journal takes them off the old account as recognized there, which only has to exist in
the chart: first what this module posted itself, then what the source journals it
registered hold, then what the ledger held at the cutover.

Both are dated the first day of the first month not yet run, so every run from then on
posts to the new account. The class keeps the accounts it used, each until that date,
for the reconciliation. Undo reverses the journal and gives the class its old account
back while nothing has happened on its assets since.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import HTTPException

from celerp.accounting_roles import refused
from celerp.services.money import to_decimal
from celerp_fixed_assets import runs, setup
from celerp_fixed_assets.projections import said
from celerp_fixed_assets.operations import MODULE, _effects, _journal_view, _post, _run, _undo
from celerp_fixed_assets.store import _emit, _entities, _state
from ui.i18n import t

FIELDS = {"asset_account": "asset", "accumulated_depreciation_account": "accumulated depreciation",
          "depreciation_account": "depreciation expense"}
BALANCE_SHEET = ("asset_account", "accumulated_depreciation_account")
MODES = ("move", "future")


def accounts_at(cls: dict, day: str) -> dict:
    """The class as it posted on ``day``: each account it has since left, back in place."""
    out = dict(cls)
    for held in sorted(cls.get("account_history") or [], key=lambda h: h["until"], reverse=True):
        if day < held["until"]:
            out[held["field"]] = held["account"]
    return out


def past_accounts(cls: dict) -> set[str]:
    return {held["account"] for held in cls.get("account_history") or [] if held.get("field") in BALANCE_SHEET}


PROBLEMS = {"posting.destination.not_in_chart": "not_in_chart", "posting.destination.inactive": "inactive",
            "posting.destination.header": "header"}
UNUSABLE = {"not_in_chart": "{asset_class} posts to account {account}, which is not in the chart of accounts.",
            "inactive": "{asset_class} posts to account {account}, which is inactive.",
            "header": "{asset_class} posts to account {account}, which is a header account with accounts under it."}


async def unusable(session, company_id, cls: dict) -> list[dict]:
    """The class's accounts the chart no longer takes postings on, each with why and as
    the user knows it (``label``: the code and the chart's name)."""
    from celerp.services.journal_accounts import require_destinations

    out = []
    for field in FIELDS:
        try:
            await require_destinations(session, company_id, {cls.get(field) or ""})
        except HTTPException as e:
            out.append({"field": field, "account": cls.get(field) or "", "problem": PROBLEMS[e.detail["message_key"]]})
    labels = await setup.account_labels(session, company_id, [p["account"] for p in out]) if out else {}
    return [{**p, "label": labels[p["account"]], "href": f"/fixed-assets/classes/{cls['id']}/fix-account?field={p['field']}"} for p in out]


async def class_refusal(session, company_id, exc: HTTPException) -> HTTPException:
    """A posting refused on an account a class posts to, said as the class's problem with
    the link to fix it: the operation's form has no account to choose. Any other refusal
    is returned as it is."""
    from celerp.accounting_roles import refusal

    detail = exc.detail if isinstance(exc.detail, dict) else {}
    code = (detail.get("params") or {}).get("code")
    if detail.get("message_key") not in (*PROBLEMS, "posting.continued_account_unusable") or not code:
        return exc
    for cls in await setup.classes(session, company_id):
        for found in await unusable(session, company_id, cls):
            if found["account"] == code:
                return HTTPException(status_code=422, detail=refusal(
                    f"fixed_assets.class_account_{found['problem']}",
                    UNUSABLE[found["problem"]].format(asset_class=cls["name"], account=found["label"]),
                    asset_class=cls["name"], account=found["label"],
                    href=found["href"], action="fixed_assets.action.fix_account"))
    return exc


async def _class_assets(session, company_id, class_id: str) -> list[tuple[str, dict]]:
    return [(eid, s) for eid, s in await _entities(session, company_id, "fa_asset")
            if s.get("class_id") == class_id and s.get("status") not in ("undone", "physical")]


def _held(assets: list[tuple[str, dict]], field: str) -> Decimal:
    """What the class's assets hold on the account in ``field``, signed as the ledger holds it."""
    if field == "asset_account":
        return sum((to_decimal(s.get("cost")) for _, s in assets), Decimal(0))
    return -sum((to_decimal(s.get("accumulated_depreciation")) + to_decimal(s.get("accumulated_impairment"))
                 for _, s in assets), Decimal(0))


async def refuse_stranding(session, company_id, class_id: str, cls: dict, changes: dict) -> None:
    """A class's balance sheet account changes only by moving what its assets hold there."""
    moving = [f for f in BALANCE_SHEET if f in changes]
    if not moving:
        return
    assets = await _class_assets(session, company_id, class_id)
    for field in moving:
        if _held(assets, field):
            raise refused(409, "fixed_assets.err_assets_name_hold_balance_account_value", name=cls['name'], value=cls[field])


def _day(books: runs.Books) -> str:
    """The first day of the first month not yet run, after any period lock."""
    month = books.next
    day = date(month // 12, month % 12 + 1, 1)
    if books.lock is not None and day <= books.lock:
        day = date.fromordinal(books.lock.toordinal() + 1)
    return day.isoformat()


async def _sources(session, company_id, assets: list[tuple[str, dict]], account: str, cut: str) -> list[str | None]:
    """Where the balance on ``account`` was recognized, in the order it is taken back."""
    from celerp.services.auto_je import live_entries
    from celerp.services.module_journals import ledger_key

    from celerp_fixed_assets.operations import asset_movements

    journals: list[str] = []
    for asset_id, _ in assets:
        for mv in await asset_movements(session, company_id, asset_id):
            for c in mv.get("contributions") or []:
                je = c.get("journal_id")
                if je and not je.startswith(f"je:mod:{MODULE}:") and je not in journals:
                    journals.append(je)
    # A source Change fund replaced is taken back from the copy standing for it.
    live = await live_entries(session, company_id, journals)
    return [None, *dict.fromkeys(live[je] for je in journals), ledger_key(cut)]


async def _split(session, company_id, account: str, amount: Decimal, keys: list[str | None],
                 debit: bool, currency: str, fund: str | None, used: dict) -> list:
    """Lines on ``fund`` taking ``amount`` off ``account``, each within what one source
    recognized there less what earlier lines of the same journal (``used``) took."""
    from celerp.services.module_journals import JournalLine, recognized_balances

    held = await recognized_balances(session, company_id, MODULE, {(account, k) for k in keys})
    lines, left = [], amount
    for key in keys:
        available = (-held[(account, key)] if debit else held[(account, key)]) - used.get(key, Decimal(0))
        take = min(left, max(available, Decimal(0)))
        if take:
            lines.append(JournalLine(account=account, debit=take if debit else Decimal(0),
                                     credit=Decimal(0) if debit else take, moves_recognized=True, recognized_in=key,
                                     fund_id=fund))
            used[key] = used.get(key, Decimal(0)) + take
            left -= take
    if left:
        raise refused(409, "fixed_assets.err_ledger_holds_said_account_account_these", said=said(amount - left, currency), account=account, said2=said(amount, currency))
    return lines


async def fix_account(session, company_id, user_id, class_id: str, body: dict) -> dict:
    """Give a class a new account in one of its account fields, moving or keeping what it holds there."""
    from celerp.services.company_lock import lock_projections
    from celerp.services.journal_accounts import require_destinations
    from celerp.services.module_journals import JournalLine

    field, account, mode = body.get("field"), str(body.get("account") or "").strip(), body.get("mode")
    if not isinstance(field, str) or field not in FIELDS:
        raise setup.refuse("fixed_assets.err_choose_asset_accumulated_depreciation_depreciation_expense")
    if not isinstance(mode, str) or mode not in MODES:
        raise setup.refuse("fixed_assets.err_choose_move_existing_balances_future_postings")
    if mode == "future" and field in BALANCE_SHEET:
        raise setup.refuse("fixed_assets.err_value_account_balance_sheet_account_so", value=FIELDS[field])
    if mode == "move" and field not in BALANCE_SHEET:
        raise setup.refuse("fixed_assets.err_depreciation_already_posted_stays_periods_was")

    async def plan(fa):
        row = (await lock_projections(session, company_id, [class_id])).get(class_id)
        if row is None or row.entity_type != "fa_class":
            raise HTTPException(status_code=404, detail=t("fixed_assets.err_no_such_asset_class"))
        cls = row.state
        old = cls.get(field)
        if not account:
            raise setup.refuse("fixed_assets.err_choose_new_account")
        if account == old:
            raise setup.refuse("fixed_assets.err_name_already_posts_account_account", name=cls['name'], account=account)
        await require_destinations(session, company_id, {account})
        assets = await _class_assets(session, company_id, class_id)
        await lock_projections(session, company_id, [a for a, _ in assets])
        books = await runs._books(session, company_id)
        day = _day(books)
        lines = []
        # What each fund's assets hold moves on that fund (rule P).
        by_fund: dict[str, list] = {}
        for asset in assets if mode == "move" else []:
            by_fund.setdefault(asset[1].get("fund_id") or "", []).append(asset)
        used: dict = {}
        for fund, group in sorted(by_fund.items()):
            held = _held(group, field)
            if not held:
                continue
            debit = held < 0
            amount = abs(held)
            keys = await _sources(session, company_id, group, old, setup.cutover(fa))
            taken = await _split(session, company_id, old, amount, keys, debit, books.currency, fund or None, used)
            new_side = JournalLine(account=account, debit=Decimal(0) if debit else amount,
                                   credit=amount if debit else Decimal(0), fund_id=fund or None)
            lines += [new_side, *taken]
        shown = {"class": cls["name"], "field": field, "old_account": old, "new_account": account, "date": day,
                 "journals": [_journal_view(lines, books.currency)] if lines else []}
        return shown, {"cls": cls, "old": old, "day": day, "lines": lines, "assets": assets,
                       "basis": [cls.get("version"), [(a, s.get("movement_count")) for a, s in assets],
                                 await runs._ledger_basis(session, company_id, books)]}

    async def apply(context, op_key):
        cls, old, day = context["cls"], context["old"], context["day"]
        journals = []
        if context["lines"]:
            journals.append(await _post(
                session, company_id, user_id, key=f"class:{class_id.removeprefix('fa:class:')}:fix:{op_key}",
                operation="fix_account", day=day, memo=f"{cls['name']} moved from account {old} to {account}",
                lines=context["lines"], source_id=class_id, href=f"/fixed-assets/classes/{class_id}",
                label=cls["name"], op_key=op_key))
        before = {field: old, "account_history": cls.get("account_history"), "last_fix": cls.get("last_fix")}
        await _emit(session, company_id, user_id, class_id, "fa_class", "fa.class.updated", {"changes": {
            field: account, "last_fix": op_key,
            "account_history": [*(cls.get("account_history") or []), {"until": day, "field": field, "account": old}]}})
        unchanged = {a: {"movement_count": int(s.get("movement_count") or 0)} for a, s in context["assets"]}
        unchanged[class_id] = {"version": int(cls.get("version") or 0) + 1}
        return ({"class_id": class_id, "field": field, "account": account, "journals": journals},
                _effects(journals=journals, classes=[{"id": class_id, "before": before}], unchanged=unchanged))

    return await _run(session, company_id, user_id, body, op_type="fix_account", keys=("manage_accounting",),
                      plan=plan, apply=apply)


async def undo_fix(session, company_id, user_id, class_id: str, body: dict) -> dict:
    """Undo the class's latest Fix posting account."""
    cls = await _state(session, company_id, class_id)
    if cls is None or not class_id.startswith("fa:class:"):
        raise HTTPException(status_code=404, detail=t("fixed_assets.err_no_such_asset_class"))
    if not cls.get("last_fix"):
        raise setup.refuse("fixed_assets.err_name_has_no_posting_account_change", name=cls['name'])
    return await _undo(session, company_id, user_id, [cls["last_fix"]], body)
