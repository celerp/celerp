# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Change fund on a document an asset was bought on (the doc_posting_dependents slot).

An asset keeps the fund of the line it was bought on (rule P). When Change fund posts a
bill's entries again on new funds, each asset bought on one of its lines takes the fund its
line now posts on (read from the bill's live entry), and every posted journal of this module
carrying the asset's value moves with it at its own date, inside the same judged action: the
asset's own journals and its registration whole, a depreciation run by the asset's share (the
credit line its movement points at and the matching expense debit). Each is voided and posted
again by module_journals.move_journal_funds. A class account move posts one line per fund for
all the class's assets and records no asset's share, so an asset such a move carried refuses
the change until that move is undone.
"""
from __future__ import annotations

from decimal import Decimal

from celerp_fixed_assets import setup, sources
from celerp_fixed_assets.fixaccount import accounts_at
from celerp_fixed_assets.operations import MODULE, asset_movements
from celerp_fixed_assets.projections import _dec
from celerp_fixed_assets.recon import _class_at
from celerp_fixed_assets.store import _emit, _entities

_PREFIX = f"je:mod:{MODULE}:"
_REASON = "Fund changed on the document this asset was bought on"


def _fund(line: dict) -> str | None:
    return line.get("fund_id") or None


def _on(line: dict, fund: str | None, **amounts) -> dict:
    out = {k: v for k, v in line.items() if k != "fund_id"} | amounts
    return out | ({"fund_id": fund} if fund else {})


def _whole(old: str | None, new: str | None):
    """Every line on ``old`` moved onto ``new``."""
    return lambda lines: [_on(ln, new) if _fund(ln) == old else ln for ln in lines]


def _shares(items: list[tuple[int, Decimal, str | None]]):
    """Each (line index, amount, fund) moving ``amount`` of that line onto ``fund``: a line
    moving whole changes its fund in place, so every other line keeps its index; a part
    moving is taken off the line and added as a new line at the end. Returns the relabel and
    the index each moved part ends at, in the order of ``items``."""
    placed: list[int] = []

    def relabel(lines: list[dict]) -> list[dict]:
        out, added = [dict(ln) for ln in lines], []
        placed.clear()
        for index, amount, fund in items:
            line = out[index]
            side = "debit" if _dec(line.get("debit")) else "credit"
            left = _dec(line.get(side))
            if left == amount:
                out[index] = _on(line, fund)
                placed.append(index)
            else:
                out[index] = {**line, side: left - amount}
                added.append(_on(line, fund, **{side: amount}))
                placed.append(len(lines) + len(added) - 1)
        return out + added

    return relabel, placed


async def dependents(session, company_id, user_id, doc_id: str, relabel) -> None:
    """Move each asset bought on a line of ``doc_id`` onto the fund the line now posts on.
    ``relabel`` rewrites the document's own legs; an asset's entries follow its line's new
    fund instead, so it is not read here."""
    from celerp.services.auto_je import live_entries
    from celerp.services.company_lock import lock_projections
    from celerp.services.journal_accounts import fund_name, funds_by_id
    from celerp.services.module_journals import move_journal_funds

    found = {f["source"]: f for f in await sources.lines(session, company_id)}
    on_doc = {s for s, f in found.items() if f["document"] == doc_id}
    if not on_doc:
        return
    bought = [aid for aid, a in await _entities(session, company_id, "fa_asset")
              if any(s.get("source") in on_doc for s in a.get("sources") or [])]
    if not bought:
        return
    rows = await lock_projections(session, company_id, bought)
    journals = await sources._posted_journals(session, company_id, prefix=_PREFIX)
    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    whole: dict[str, tuple[str | None, str | None]] = {}
    shares: dict[str, list[tuple[str, int, int, Decimal, str | None, str | None]]] = {}
    moving: dict[str, tuple[dict, str | None, list[dict]]] = {}
    for asset_id in sorted(bought):
        asset = dict(rows[asset_id].state or {})
        funds = {_fund(found[s["source"]]) for s in asset.get("sources") or [] if s.get("source") in found}
        old = asset.get("fund_id") or None
        if len(funds) > 1:
            names = await funds_by_id(session, company_id)
            raise setup.refuse("fixed_assets.err_change_fund_splits_asset", number=asset["number"],
                               funds=", ".join(sorted(fund_name(names, f) for f in funds)))
        new = funds.pop() if funds else old
        if new == old:
            continue
        movements = await asset_movements(session, company_id, asset_id)
        moving[asset_id] = (asset, new, movements)
        first = min((m.get("posting_date") or "" for m in movements), default="")
        own = {asset_id, *(sources.claim_id(s["source"]) for s in asset.get("sources") or [])}
        past = {asset.get("class_id"), *(h.get("class_id") for h in asset.get("class_history") or [])}
        for je_id, je in journals.items():
            source = (je.get("source") or {}).get("entity_id")
            if source in own:
                whole[je_id] = (old, new)
            elif (source in past and str(je.get("ts") or "")[:10] >= first[:10]
                  and any(_fund(e) == old for e in je.get("entries") or [])):
                raise setup.refuse("fixed_assets.err_change_fund_class_move", number=asset["number"],
                                   cls=classes[source]["name"], date=str(je.get("ts") or "")[:10])
        contributed = [(m, n, c) for m in movements for n, c in enumerate(m.get("contributions") or [])
                       if str(c.get("journal_id") or "").startswith(_PREFIX)]
        live = await live_entries(session, company_id, [c["journal_id"] for _, _, c in contributed])
        for m, n, c in contributed:
            je_id = live[c["journal_id"]]
            je = journals.get(je_id)
            if je is None or je_id in whole or not str((je.get("source") or {}).get("entity_id")).startswith("fa:run:"):
                continue
            entries = je.get("entries") or []
            index = int(c["line_index"])
            if _fund(entries[index]) != old:
                continue
            day = str(je.get("ts") or "")[:10]
            expense = asset.get("depreciation_account") or accounts_at(
                classes[_class_at(asset, day)], day)["depreciation_account"]
            debit = next(i for i, e in enumerate(entries)
                         if e.get("account") == expense and _fund(e) == old and _dec(e.get("debit")))
            amount = _dec(c.get("amount"))
            shares.setdefault(je_id, []).extend([(m["id"], n, index, amount, old, new),
                                                 ("", -1, debit, amount, old, new)])

    for asset_id, (_, new, _) in moving.items():
        await _emit(session, company_id, user_id, asset_id, "fa_asset", "fa.asset.updated",
                    {"changes": {"fund_id": new}})
    for je_id, (old, new) in sorted(whole.items()):
        await move_journal_funds(session, company_id=company_id, user_id=user_id, module=MODULE, je_id=je_id,
                                 reason=_REASON, relabel=_whole(old, new))
    for je_id, items in sorted(shares.items()):
        relabel_run, placed = _shares([(index, amount, new) for _, _, index, amount, _, new in items])
        await move_journal_funds(session, company_id=company_id, user_id=user_id, module=MODULE, je_id=je_id,
                                 reason=_REASON, relabel=relabel_run)
        # A part added at the end: the movement's contribution now points at it.
        repointed: dict[str, dict[int, int]] = {}
        for (movement, n, index, _, _, _), at in zip(items, placed):
            if movement and at != index:
                repointed.setdefault(movement, {})[n] = at
        for movement, points in repointed.items():
            mv = next(m for _, _, ms in moving.values() for m in ms if m["id"] == movement)
            contributions = [{**c, "line_index": points[n]} if n in points else c
                             for n, c in enumerate(mv.get("contributions") or [])]
            await _emit(session, company_id, user_id, movement, "fa_movement", "fa.movement.updated",
                        {"changes": {"contributions": contributions}})
