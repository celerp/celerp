# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Document lines that sell a fixed asset (an invoice) or credit its sale (a credit note).

Core checks what any document can check about such a line, and hands every write,
posting and reversal of a document carrying one to the module that keeps the assets
(the doc_asset_lines slot), inside the same transaction: a refusal there refuses the
document action.
"""
from __future__ import annotations

from fastapi import HTTPException

from celerp.services.line_measures import line_label
from celerp.services.money import to_decimal
from ui.i18n import t

# The documents a line may sell or credit an asset on.
ASSET_DOC_TYPES = ("invoice", "credit_note")


def asset_lines(doc: dict) -> list[dict]:
    """The document's lines that name an asset."""
    return [li for li in doc.get("line_items") or [] if isinstance(li, dict) and li.get("asset_id")]


def _refuse(key: str, line: dict, index: int) -> None:
    """Refuse (422) naming the line as every document message names it, else by its position."""
    raise HTTPException(status_code=422, detail=t(key, line=line_label(line) or index + 1))


async def notify(session, *, company_id, user_id, doc_id: str, doc: dict, change: str) -> None:
    """Hand a change to a document carrying asset lines to the module that keeps the assets.
    Refused (409) when no module is there to take it, so no asset line is ever written or
    posted without its asset following."""
    if not asset_lines(doc):
        return
    from celerp.modules.slots import get as get_slot, resolve_handler

    handlers = sorted({c["handler"] for c in get_slot("doc_asset_lines")})
    if not handlers:
        raise HTTPException(status_code=409, detail=t("documents.err_asset_lines_unhandled"))
    for handler in handlers:
        await resolve_handler(handler)(session=session, company_id=company_id, user_id=user_id,
                                       doc_id=doc_id, doc=doc, change=change)


async def check_write(session, *, company_id, user_id, doc_id: str, doc: dict, before: dict) -> None:
    """Check a write that takes a document from ``before`` to ``doc``; every writer of a
    document's lines, type or original document comes here (emit_event).

    Refuse (422, naming the line) asset lines a document cannot carry: an asset id that is
    not text, a document that is not an invoice or credit note, the same asset twice, a
    quantity other than one, or a credit note line that does not credit the same asset's
    line on its invoice. An issued document's assets never change (409); its postings
    follow it through the posting hooks instead. On a draft, the module is told of the
    asset lines the write takes off (notify, change "unlink") and then checks the assets
    it carries (change "write")."""
    lines = doc.get("line_items") or []
    named = [(i, li) for i, li in enumerate(lines) if isinstance(li, dict) and li.get("asset_id")]
    for i, li in named:
        if not isinstance(li["asset_id"], str):
            _refuse("documents.err_asset_line_id", li, i)
    gone = {li["asset_id"] for li in asset_lines(before)} - {li["asset_id"] for _, li in named}
    if not named and not gone:
        return
    if (doc.get("status") or "draft") != "draft":
        if sorted(li["asset_id"] for _, li in named) != sorted(li["asset_id"] for li in asset_lines(before)):
            raise HTTPException(status_code=409, detail=t("documents.err_asset_lines_issued"))
        return
    if gone:
        await notify(session, company_id=company_id, user_id=user_id, doc_id=doc_id,
                     doc={**before, "line_items": [li for li in asset_lines(before) if li["asset_id"] in gone]},
                     change="unlink")
    if not named:
        return
    if doc.get("doc_type") not in ASSET_DOC_TYPES:
        _refuse("documents.err_asset_line_doc_type", *named[0][::-1])
    seen: set[str] = set()
    for i, li in named:
        if li["asset_id"] in seen:
            _refuse("documents.err_asset_line_twice", li, i)
        seen.add(li["asset_id"])
        if to_decimal(li.get("quantity") or 0) != 1:
            _refuse("documents.err_asset_line_quantity", li, i)
    if doc.get("doc_type") == "credit_note":
        from celerp.models.projections import Projection

        original = await session.get(Projection, {"company_id": company_id,
                                                  "entity_id": str(doc.get("original_doc_id") or "")})
        sources = {li.get("line_id"): li for li in ((original.state or {}) if original else {}).get("line_items") or []
                   if isinstance(li, dict) and li.get("line_id")}
        for i, li in named:
            source = sources.get(li.get("source_line_id"))
            if source is None or source.get("asset_id") != li["asset_id"]:
                _refuse("documents.err_asset_line_source", li, i)
    await notify(session, company_id=company_id, user_id=user_id, doc_id=doc_id, doc=doc, change="write")
