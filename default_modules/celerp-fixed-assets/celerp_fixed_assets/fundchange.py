# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Change fund on a document an asset was bought on (the doc_posting_dependents slot).

An asset keeps the fund of the lines carrying its value (rule P): the lines its claims name,
its registration, an improvement or a link, and for an asset placed in service from a site,
the site's lines. When Change fund posts a document's entries again
on new funds, a journal this module posted from one of its lines (a registration's reclass, an
expense or maintenance) follows the line's new fund, and each asset one of its lines carries
takes that fund, read from the document's live entry. Every posted journal of this module
carrying the asset's value moves with it at its own date, inside the same judged action: the
asset's own journals whole, a depreciation run by the asset's share (the credit line its
movement points at and the matching expense debit). Each is voided and posted again by
module_journals.move_journal_funds. An asset whose lines would end up on different funds
refuses the change. A class account move posts one line per fund for all the class's assets
and records no asset's share, so an asset such a move carried refuses the change until that
move is undone.
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
_EXPENSED = ("expense", "maintenance")
_UNSET = object()


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
    """Move the journals posted from ``doc_id``'s lines and each asset those lines carry onto
    the fund the line now posts on. ``relabel`` rewrites the document's own legs; an asset's entries follow its line's new
    fund instead, so it is not read here."""
    from celerp.services.auto_je import live_entries
    from celerp.services.company_lock import lock_projections
    from celerp.services.journal_accounts import fund_name, funds_by_id
    from celerp.services.module_journals import move_journal_funds

    found = {f["source"]: f for f in await sources.lines(session, company_id)}
    on_doc = {s for s, f in found.items() if f["document"] == doc_id and f["claim"]}
    if not on_doc:
        return
    # The lines carrying each asset's value: every claimed line naming it, the registration,
    # an improvement, a link, except a line registered as an expense or maintenance.
    fed: dict[str, set[str]] = {}
    for s, f in found.items():
        if f["claim"] and f["claim"].get("allocation") not in _EXPENSED:
            for aid in f["claim"].get("asset_ids") or []:
                fed.setdefault(aid, set()).add(s)
    # An asset placed in service from a site carries the site's cost, so the site's lines.
    placed = {aid: a["placed_from"] for aid, a in await _entities(session, company_id, "fa_asset")
              if a.get("placed_from")}
    while grown := {aid for aid, site in placed.items() if fed.get(site, set()) - fed.get(aid, set())}:
        for aid in grown:
            fed.setdefault(aid, set()).update(fed[placed[aid]])
    bought = sorted(aid for aid, lines in fed.items() if lines & on_doc)
    rows = await lock_projections(session, company_id, bought) if bought else {}
    journals = await sources._posted_journals(session, company_id, prefix=_PREFIX)
    classes = {c["id"]: c for c in await setup.classes(session, company_id, include_archived=True)}
    # A class account move posts one line per fund for every asset of the class; its operation
    # names the assets it carried.
    fixes = [op.get("effects") or {} for _, op in await _entities(session, company_id, "fa_op")
             if op.get("type") == "fix_account"]
    whole: dict[str, tuple[str | None, str | None]] = {}
    shares: dict[str, list[tuple[str, int, int, Decimal, str | None, str | None]]] = {}
    moving: dict[str, tuple[dict, str | None, list[dict]]] = {}
    # A journal posted from one of the document's lines (a registration's reclass, an expense
    # or maintenance) is on that line's fund: it follows the line.
    by_claim = {sources.claim_id(s): _fund(found[s]) for s in on_doc}
    for je_id, je in journals.items():
        new = by_claim.get((je.get("source") or {}).get("entity_id"), _UNSET)
        if new is _UNSET:
            continue
        olds = {_fund(e) for e in je.get("entries") or []} - {new}
        if olds:
            whole[je_id] = (olds.pop(), new)
    for asset_id in bought:
        asset = dict(rows[asset_id].state or {})
        funds = {_fund(found[s]) for s in fed[asset_id]}
        old = asset.get("fund_id") or None
        if len(funds) > 1:
            names = await funds_by_id(session, company_id)
            raise setup.refuse("fixed_assets.err_change_fund_splits_asset", number=asset["number"],
                               funds=", ".join(sorted(fund_name(names, f) for f in funds)))
        new = funds.pop()
        if new == old:
            continue
        movements = await asset_movements(session, company_id, asset_id)
        moving[asset_id] = (asset, new, movements)
        for je_id, je in journals.items():
            if (je.get("source") or {}).get("entity_id") == asset_id:
                whole[je_id] = (old, new)
        for effects in fixes:
            if asset_id not in (effects.get("unchanged") or {}):
                continue
            for je_id in effects.get("journals") or []:
                je = journals.get(je_id)
                if je is not None and any(_fund(e) == old for e in je.get("entries") or []):
                    raise setup.refuse("fixed_assets.err_change_fund_class_move", number=asset["number"],
                                       cls=classes[je["source"]["entity_id"]]["name"], date=str(je.get("ts") or "")[:10])
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
