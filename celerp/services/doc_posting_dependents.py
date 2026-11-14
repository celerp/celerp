# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""What a document's postings carry into other records: a schedule recognizing its income,
an asset bought on one of its lines. When Change fund posts a document's entries again on
new funds, each module keeping such a record moves it with them (the doc_posting_dependents
slot), inside the same open fund change, so the whole action is judged once
(journal_accounts.judge_fund_change).
"""
from __future__ import annotations

from collections.abc import Callable


async def move(session, *, company_id, user_id, doc_id: str, relabel: Callable[[list[dict]], list[dict]]) -> None:
    """Hand a document's fund change to every module keeping a record that copied a fund from
    it. Runs only inside an open fund change (RuntimeError otherwise), so no dependent entry
    is voided or posted again unjudged."""
    from celerp.modules.slots import get as get_slot, resolve_handler
    from celerp.services.journal_accounts import open_fund_change

    if open_fund_change(session) is None:
        raise RuntimeError("Dependents move only inside an open fund change")
    for handler in sorted({c["handler"] for c in get_slot("doc_posting_dependents")}):
        await resolve_handler(handler)(session=session, company_id=company_id, user_id=user_id,
                                       doc_id=doc_id, relabel=relabel)
