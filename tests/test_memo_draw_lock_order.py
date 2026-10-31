# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every writer that touches a memo, its invoices or their lots takes the company row lock
first, so two writers never wait on each other in opposite orders. The order is read from the
SQL each writer sends."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import event

from memo_draw_support import auth, back_to_memo, contact, draft, draw, drawn, drawn_lines, memo_out, ok, pay, rate, state, service_line

pytestmark = pytest.mark.asyncio

_LOCK = re.compile(r"FOR (NO KEY UPDATE|UPDATE|SHARE|KEY SHARE)", re.I)
_RANK = {"contact": 0, "invoice": 1, "memo": 2, "lot": 3}


class _Recorder:
    """Records each row lock a writer takes, in order, labelled by the row it names."""

    def __init__(self, session, labels: dict):
        self.engine = session.bind.engine.sync_engine
        self.labels, self.seen = labels, []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, params, context, executemany):
        found = _LOCK.search(statement)
        if not found:
            return
        mode = found.group(1).upper().replace(" ", "_")
        table = re.search(r"\bFROM (\w+)", statement)
        if table and table.group(1) == "companies":
            name = f"company:{mode}"
        else:
            flat = " ".join(map(str, params if isinstance(params, (list, tuple)) else (params or {}).values()))
            hit = [lab for key, lab in self.labels.items() if key in flat]
            name = f"{'+'.join(dict.fromkeys(hit)) or 'other'}:{mode}"
        if not self.seen or self.seen[-1] != name:
            self.seen.append(name)


def _assert_company_first(r, seen: list[str]) -> None:
    assert r.status_code in (200, 204), r.text
    assert seen and seen[0] == "company:NO_KEY_UPDATE", seen


def _assert_new_writer_order(r, seen: list[str]) -> None:
    """Company first, then contact, invoices, memos, lots, each where it is first locked: a
    service the writer calls later in the same transaction locks rows it already holds again,
    which the database grants at once."""
    _assert_company_first(r, seen)
    first = dict.fromkeys(name.split(":")[0] for name in seen[1:])
    ranks = [_RANK[name] for name in first if name in _RANK]
    assert ranks == sorted(ranks), seen


async def _memo(client, h, cid=None, **header):
    memo, lids, lots = await memo_out(client, h, [(2, 100.0)], lots_per_line=2, contact_id=cid, **header)
    return memo, lids, {i: "lot" for i in lots[0]}


# ── Writers reused by the draw, kept as guards ────────────────────────────────

@pytest.mark.parametrize("writer", ["patch_lines", "finalize", "void_from_memo", "revert_lines", "create_with_contact", "memo_payment"])
async def test_writers_take_company_lock_first(client, session, writer):
    h = await auth(session)
    memo, lids, labels = await _memo(client, h)
    labels[memo] = "memo"
    if writer in ("patch_lines", "void_from_memo"):
        inv = (await ok(await client.post(f"/docs/{memo}/convert", headers=h)))["target_doc_id"]
        labels[inv] = "invoice"
    if writer == "patch_lines":
        st = await state(client, h, inv)
        lines = [{**st["line_items"][0], "description": "Edited"}, *st["line_items"][1:]]
        call = lambda: client.patch(f"/docs/{inv}", headers=h, json={"fields_changed": {"line_items": {"new": lines}}, "expected_version": st["version"]})
    elif writer == "finalize":
        d = await draft(client, h, lines=[service_line()])
        labels[d] = "invoice"
        call = lambda: client.post(f"/docs/{d}/finalize", headers=h)
    elif writer == "void_from_memo":
        call = lambda: client.post(f"/docs/{inv}/void", headers=h, json={"reason": "Lock order"})
    elif writer == "revert_lines":
        call = lambda: client.post(f"/docs/{memo}/revert-lines", headers=h, json={"line_ids": lids[:1]})
    elif writer == "create_with_contact":
        cid = await contact(client, h, "Lock order store")
        labels[cid] = "contact"
        call = lambda: client.post("/docs", headers=h, json={
            "doc_type": "invoice", "contact_id": cid, "line_items": [service_line()]})
    else:
        call = lambda: pay(client, h, memo, 10.0)

    with _Recorder(session, labels) as rec:
        r = await call()

    _assert_company_first(r, rec.seen)


# ── The new writers ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("writer", ["draw", "back_to_memo", "rate_correction"])
async def test_writers_take_company_lock_first_new_writers(client, session, writer):
    h = await auth(session)
    cid = await contact(client, h, "Lock order store")
    # The rate correction needs a tax amount with no rate; a draw of all of it onto a draft
    # with lines is refused for one, so the draw's memo has none.
    memo, lids, labels = await _memo(client, h, cid, **({} if writer == "draw" else {"tax": 35.0}))
    labels.update({memo: "memo", cid: "contact"})
    if writer == "draw":
        d = await draft(client, h, contact_id=cid, lines=[service_line()])
        labels[d] = "invoice"
        call = lambda: draw(client, h, memo, line_ids=lids, target=d)
    elif writer == "back_to_memo":
        inv = await drawn(client, h, memo, line_ids=lids)
        labels[inv] = "invoice"
        back = [li["line_id"] for li in drawn_lines(await state(client, h, inv))]
        call = lambda: back_to_memo(client, h, inv, back)
    else:
        binding = (await ok(await rate(client, h, memo, 8, dry_run=True)))["binding"]
        call = lambda: rate(client, h, memo, 8, binding=binding)

    with _Recorder(session, labels) as rec:
        r = await call()

    _assert_new_writer_order(r, rec.seen)
