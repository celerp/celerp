# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Change fund on a posted record that came from outside: the record's own void, then the
same entries posted again with the new fund, all in one transaction and judged once over
everything the action voids or posts.

Initial conditions per test: a fresh company with the seeded chart. ``_setup`` turns funds on
with a restricted fund R (purpose Scholarships), two unrestricted funds and the restricted net
assets account. Outside records come in through the document and journal imports (origin
``file_import``) with their fund chosen on the import; restricted money is put on R by a gift
journal emitted directly.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from celerp.models.ledger import LedgerEntry
from celerp.models.projections import Projection
from test_fund_movement_posting import _held, _set_role
from test_fund_movements import _emit, _setup
from test_income_accounts import _item, _line
from test_outside_lines import _doc, _issued_import

pytestmark = pytest.mark.asyncio

REASON = "Charged to the wrong grant"


def _key(r) -> str:
    return r.json()["detail"]["message_key"]


async def _state(session, auth, entity_id) -> dict | None:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": entity_id})
    return row.state if row is not None else None


async def _version(session, auth, entity_id) -> int:
    session.expire_all()
    row = await session.get(Projection, {"company_id": auth["company_id"], "entity_id": entity_id})
    return row.version


async def _outside_invoice(client, auth, fund: str, amounts=(40, 60)) -> str:
    """An open invoice brought in by the document import, every line on ``fund``; it posts on import."""
    items = [await _item(client, auth, f"Outside {n}") for n in range(len(amounts))]
    doc = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={
        "records": [_issued_import(doc, [_line(i, f"Outside {n}", a) for n, (i, a) in enumerate(zip(items, amounts))])],
        "fund_choices": {"records": {doc: fund}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    return doc


async def _outside_journal(client, auth, lines: list[tuple[str, float, float]], fund: str, ts="2026-03-01") -> str:
    """A posted journal brought in by the journal import, every line on ``fund``."""
    je = f"je:{uuid.uuid4()}"
    r = await client.post("/accounting/import/batch", headers=auth["headers"], json={
        "records": [{"entity_id": je, "event_type": "acc.journal_entry.created", "source": "csv",
                     "idempotency_key": uuid.uuid4().hex,
                     "data": {"status": "posted", "ts": ts, "memo": "Filed",
                              "entries": [{"account": a, "debit": d, "credit": c} for a, d, c in lines]}}],
        "fund_choices": {"all": fund}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    return je


async def _change_doc(client, session, auth, doc, *, version=None, request_key=None, reason=REASON, **choices):
    if version is None:
        version = (await _doc(client, auth, doc))["version"]
    return await client.post(f"/docs/{doc}/change-fund", headers=auth["headers"], json={
        "expected_version": version, "request_key": uuid.uuid4().hex if request_key is None else request_key, "reason": reason, **choices})


async def _change_journal(client, session, auth, je, *, version=None, request_key=None, reason=REASON, **choices):
    if version is None:
        version = await _version(session, auth, je)
    return await client.post(f"/accounting/journal/{je}/change-fund", headers=auth["headers"], json={
        "expected_version": version, "request_key": uuid.uuid4().hex if request_key is None else request_key, "reason": reason, **choices})


def _shape(entries: list[dict]) -> list[tuple]:
    """Accounts and amounts, order free, fund ignored."""
    return sorted((e["account"], float(e.get("debit") or 0), float(e.get("credit") or 0)) for e in entries)


def _funds_of(entries: list[dict]) -> set:
    return {e.get("fund_id") for e in entries}


async def _ledger(session, auth, entity_prefix: str) -> list[LedgerEntry]:
    session.expire_all()
    return list((await session.scalars(select(LedgerEntry).where(
        LedgerEntry.company_id == auth["company_id"],
        LedgerEntry.entity_id.startswith(entity_prefix, autoescape=True)).order_by(LedgerEntry.id))).all())


# ── the action ─────────────────────────────────────────────────────────────────────────

async def test_change_fund_voids_and_reposts_in_one_transaction(client, session, auth, monkeypatch):
    """An outside invoice on R changed to Unrestricted: its entry is voided in one numbered
    batch and posted again at the restore id on the same date, accounts and amounts the same,
    only the fund changed; the change is recorded with its reason; changing back restores R;
    a failure after the void rolls the whole change back."""
    f = await _setup(client, auth)
    doc = await _outside_invoice(client, auth, f["R"])
    original = await _state(session, auth, f"je:auto:{doc}:fin")
    assert _funds_of(original["entries"]) == {f["R"]}

    r = await _change_doc(client, session, auth, doc, document="unrestricted")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, f"je:auto:{doc}:fin"))["status"] == "void"
    copy = await _state(session, auth, f"je:auto:{doc}:fin:unvoid:1")
    assert copy["status"] == "posted" and copy["ts"] == original["ts"]
    assert _shape(copy["entries"]) == _shape(original["entries"]) and _funds_of(copy["entries"]) == {None}
    voids = [e for e in await _ledger(session, auth, f"je:auto:{doc}:fin")
             if e.event_type == "acc.journal_entry.voided"]
    assert [(e.metadata_["trigger"], e.metadata_["void_batch"], e.data["replaced_by"]) for e in voids] == [
        ("fund_change", 1, f"je:auto:{doc}:fin:unvoid:1")]
    changed = [e for e in await _ledger(session, auth, doc) if e.event_type == "doc.fund_changed"]
    assert len(changed) == 1 and changed[0].data["reason"] == REASON
    assert (await _doc(client, auth, doc))["fund_choice"] == "unrestricted"

    r = await _change_doc(client, session, auth, doc, document=f["R"], reason="Back to the grant")
    assert r.status_code == 200, r.text
    assert (await _state(session, auth, f"je:auto:{doc}:fin:unvoid:1"))["status"] == "void"
    back = await _state(session, auth, f"je:auto:{doc}:fin:unvoid:2")
    assert _funds_of(back["entries"]) == {f["R"]} and _shape(back["entries"]) == _shape(original["entries"])

    # A failure after the void keeps the live entry posted and writes nothing.
    import celerp.services.auto_je as auto_je

    async def _boom(*_a, **_k):
        raise RuntimeError("injected after the void")

    monkeypatch.setattr(auto_je, "repost_record_postings", _boom)
    before = len(await _ledger(session, auth, f"je:auto:{doc}:"))
    with pytest.raises(RuntimeError):
        await _change_doc(client, session, auth, doc, document="unrestricted")
    await session.rollback()
    assert (await _state(session, auth, f"je:auto:{doc}:fin:unvoid:2"))["status"] == "posted"
    assert len(await _ledger(session, auth, f"je:auto:{doc}:")) == before


async def test_change_fund_refusals_isolated(client, session, auth):
    """Each refusal in check order writes nothing; its twin changes the fund."""
    f = await _setup(client, auth)
    doc = await _outside_invoice(client, auth, f["R"])
    fin = f"je:auto:{doc}:fin"

    async def untouched():
        assert (await _state(session, auth, fin))["status"] == "posted"
        assert await _state(session, auth, f"{fin}:unvoid:1") is None

    r = await _change_doc(client, session, auth, doc, request_key="", document="unrestricted")
    assert r.status_code == 422, r.text
    await untouched()
    r = await _change_doc(client, session, auth, doc, reason=" ", document="unrestricted")
    assert r.status_code == 422 and _key(r) == "posting.fund.change_reason_missing", r.text
    await untouched()
    r = await _change_doc(client, session, auth, doc, version=0, document="unrestricted")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "stale_version", r.text
    await untouched()
    r = await _change_doc(client, session, auth, doc, document="fund:none")
    assert r.status_code == 422 and _key(r) == "posting.fund.document_choice_missing", r.text
    await untouched()

    # Not outside: a local invoice; not posted: an outside draft.
    item = await _item(client, auth, "Local")
    local = (await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "line_items": [_line(item, "Local", 10)],
        "total": 10})).json()["id"]
    r = await _change_doc(client, session, auth, local, document="unrestricted")
    assert r.status_code == 422 and _key(r) == "posting.fund.change_not_outside", r.text

    # Settled: a payment stands against it.
    paid = await _outside_invoice(client, auth, f["R"])
    pay = await client.post(f"/docs/{paid}/payment", headers=auth["headers"], json={
        "amount": 30, "payment_date": "2026-03-02", "bank_account": "1111", "idempotency_key": uuid.uuid4().hex})
    assert pay.status_code == 200, pay.text
    r = await _change_doc(client, session, auth, paid, document="unrestricted")
    assert r.status_code == 422 and _key(r) == "posting.fund.change_settled", r.text
    assert (await _state(session, auth, f"je:auto:{paid}:fin"))["status"] == "posted"

    # Closed period of the entry.
    lock = await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": "2026-01-31"})
    assert lock.status_code == 200, lock.text
    r = await _change_doc(client, session, auth, doc, document="unrestricted")
    assert r.status_code == 422, r.text
    await untouched()
    await client.post("/accounting/period-lock", headers=auth["headers"], json={"lock_date": None})

    # Permission: an operator has no manage_accounting.
    await _set_role(session, auth, "operator")
    r = await _change_doc(client, session, auth, doc, document="unrestricted")
    assert r.status_code == 403, r.text
    await untouched()
    await _set_role(session, auth, "admin")

    r = await _change_doc(client, session, auth, doc, document="unrestricted")
    assert r.status_code == 200, r.text

    # Funds off: the route refuses before anything else.
    off = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": False})
    if off.status_code == 200:
        r = await _change_doc(client, session, auth, doc, document=f["R"])
        assert r.status_code == 422 and _key(r) == "fund.not_enabled", r.text


async def test_change_fund_concurrent_and_repeat_safe(client, session, auth):
    """A second change from the same copy is refused stale; a repeat of the first request key,
    carrying the now old version, answers with the first result and writes nothing more."""
    f = await _setup(client, auth)
    doc = await _outside_invoice(client, auth, f["R"])
    version = (await _doc(client, auth, doc))["version"]
    key = uuid.uuid4().hex
    first = await _change_doc(client, session, auth, doc, version=version, request_key=key, document="unrestricted")
    assert first.status_code == 200, first.text
    other = await _change_doc(client, session, auth, doc, version=version, document=f["U1"])
    assert other.status_code == 409 and other.json()["detail"]["code"] == "stale_version", other.text
    count = len(await _ledger(session, auth, f"je:auto:{doc}:"))
    again = await _change_doc(client, session, auth, doc, version=version, request_key=key, document="unrestricted")
    assert again.status_code == 200 and again.json() == first.json(), again.text
    assert len(await _ledger(session, auth, f"je:auto:{doc}:")) == count
    assert await _state(session, auth, f"je:auto:{doc}:fin:unvoid:2") is None


# ── the fund rule over the whole action ───────────────────────────────────────────────

async def test_change_fund_cost_onto_restricted_posts(client, session, auth):
    """A manager moves an outside cost onto R while R holds enough: posts, R's bank down 300
    and the unrestricted group's up 300 (from -300 to 0). Onto R holding 100: refused by the balance rule.
    Back to Unrestricted (R only gains): posts with manage_accounting alone."""
    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 1000, 0, f["R"]), ("4100", 0, 1000, f["R"])])
    await session.commit()
    je = await _outside_journal(client, auth, [("6200", 300, 0), ("1111", 0, 300)], "unrestricted")
    await _set_role(session, auth, "manager")
    r = await _change_journal(client, session, auth, je, all=f["R"])
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == Decimal(700) and await _held(session, auth, None) == Decimal(0)
    assert _funds_of((await _state(session, auth, f"{je}:fund:1"))["entries"]) == {f["R"]}

    poor = await _outside_journal(client, auth, [("6200", 800, 0), ("1111", 0, 800)], "unrestricted")
    r = await _change_journal(client, session, auth, poor, all=f["R"])
    assert r.status_code == 422 and _key(r) == "posting.fund.change_exceeds_balance", r.text
    assert (await _state(session, auth, poor))["status"] == "posted"

    r = await _change_journal(client, session, auth, f"{je}:fund:1", all="unrestricted")
    assert r.status_code == 200, r.text
    assert await _held(session, auth, f["R"]) == Decimal(1000)


async def test_change_fund_judged_once_over_the_action(client, session, auth):
    """(d) An outside gift on R moved to Unrestricted while R holds 40 on the bank is refused by
    the balance rule; with the balance it posts. Moving it to another restricted fund needs
    Reallocate restricted funds and the same purpose."""
    f = await _setup(client, auth)
    other = (await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": "Library roof", "restricted": True, "purpose": "Roof"})).json()["id"]
    same = (await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": "Scholarships two", "restricted": True,
        "purpose": "Scholarships"})).json()["id"]
    gift = await _outside_journal(client, auth, [("1111", 100, 0), ("4100", 0, 100)], f["R"])
    await _emit(session, auth, [("6200", 60, 0, f["R"]), ("1111", 0, 60, f["R"])])
    await session.commit()
    assert await _held(session, auth, f["R"]) == Decimal(40)

    r = await _change_journal(client, session, auth, gift, all="unrestricted")
    assert r.status_code == 422 and _key(r) == "posting.fund.change_exceeds_balance", r.text
    assert (await _state(session, auth, gift))["status"] == "posted"

    await _emit(session, auth, [("1111", 500, 0, f["R"]), ("4100", 0, 500, f["R"])])
    await session.commit()
    await _set_role(session, auth, "manager")
    r = await _change_journal(client, session, auth, gift, all=other)
    assert r.status_code == 422 and _key(r) == "posting.fund.change_needs_reallocation", r.text
    r = await _change_journal(client, session, auth, gift, all=same)
    assert r.status_code == 422 and _key(r) == "posting.fund.change_needs_reallocation", r.text
    await _set_role(session, auth, "admin")
    r = await _change_journal(client, session, auth, gift, all=other)
    assert r.status_code == 422 and _key(r) == "posting.fund.change_needs_reallocation", r.text
    r = await _change_journal(client, session, auth, gift, all=same)
    assert r.status_code == 200, r.text
    changed = [e for e in await _ledger(session, auth, gift) if e.event_type == "acc.journal_entry.fund_changed"]
    assert changed[-1].data["authorized_by"] == str(auth["user_id"])

    r = await _change_journal(client, session, auth, f"{gift}:fund:1", all="unrestricted")
    assert r.status_code == 200, r.text
    assert await _held(session, auth, None) == Decimal(100)


async def test_fund_change_action_counts_every_journal_event(client, session, auth):
    """A journal event emitted directly inside the action is counted: moving R's gift to a
    restricted fund of another purpose is refused on leaving the action. A relabel or a
    dependents call with no open action raises; another session's event is not in the list."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from celerp.events.engine import emit_event
    from celerp.services import doc_posting_dependents
    from celerp.services.auto_je import repost_record_postings
    from celerp.services.journal_accounts import RestrictedFundMovement, fund_change_action

    f = await _setup(client, auth)
    roof = (await client.post("/accounting/funds", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "name": "Library roof", "restricted": True, "purpose": "Roof"})).json()["id"]
    gift = await _emit(session, auth, [("1111", 100, 0, f["R"]), ("4100", 0, 100, f["R"])])
    await session.commit()
    cid, uid = auth["company_id"], auth["user_id"]

    with pytest.raises(RestrictedFundMovement) as refused:
        async with fund_change_action(session, cid, uid) as action:
            await emit_event(session, company_id=cid, entity_id=gift, entity_type="journal_entry",
                             event_type="acc.journal_entry.voided", data={"reason": "moved", "ts": "2026-03-01"},
                             actor_id=uid, location_id=None, source="test", idempotency_key=f"t:{gift}:v")
            await emit_event(session, company_id=cid, entity_id=f"{gift}:moved", entity_type="journal_entry",
                             event_type="acc.journal_entry.created",
                             data={"memo": "moved", "ts": "2026-03-01", "je_type": "manual", "status": "posted",
                                   "entries": [{"account": "1111", "debit": 100, "credit": 0, "fund_id": roof},
                                               {"account": "4100", "debit": 0, "credit": 100, "fund_id": roof}]},
                             actor_id=uid, location_id=None, source="test", idempotency_key=f"t:{gift}:c")
            other = async_sessionmaker(bind=session.bind, class_=AsyncSession,
                                       join_transaction_mode="create_savepoint")()
            assert other.info.get(type(action).__name__) is None
            await other.close()
            assert [eid for eid, _ in action.events] == [gift, f"{gift}:moved"]
    assert refused.value.detail["message_key"] == "posting.fund.change_needs_reallocation"
    await session.rollback()
    assert (await _state(session, auth, gift))["status"] == "posted"
    assert await _state(session, auth, f"{gift}:moved") is None

    with pytest.raises(RuntimeError):
        await repost_record_postings(session, company_id=cid, user_id=uid, doc_id="doc:none", batch=1,
                                     relabel=lambda entries: entries, trigger="fund_change")
    with pytest.raises(RuntimeError):
        await doc_posting_dependents.move(session, company_id=cid, user_id=uid, doc_id="doc:none",
                                          relabel=lambda entries: entries)


async def test_commit_inside_fund_change_action_refused(client, session, auth):
    """A commit inside the open action raises and nothing it wrote is kept; a savepoint still
    releases; after the action closes the caller's commit succeeds."""
    from celerp.events.engine import emit_event
    from celerp.services.journal_accounts import fund_change_action

    f = await _setup(client, auth)
    cid, uid = auth["company_id"], auth["user_id"]
    gift = await _emit(session, auth, [("1111", 100, 0, None), ("4100", 0, 100, None)])
    await session.commit()
    with pytest.raises(RuntimeError):
        async with fund_change_action(session, cid, uid):
            await emit_event(session, company_id=cid, entity_id=gift, entity_type="journal_entry",
                             event_type="acc.journal_entry.voided", data={"reason": "x", "ts": "2026-03-01"},
                             actor_id=uid, location_id=None, source="test", idempotency_key=f"t:{gift}:v")
            async with session.begin_nested():
                pass
            await _outer_commit(session)
    await session.rollback()
    assert (await _state(session, auth, gift))["status"] == "posted"

    async with fund_change_action(session, cid, uid):
        async with session.begin_nested():
            pass
    await session.commit()
    assert f["R"]


async def _outer_commit(session):
    """The commit a misbehaving step would run: the outer transaction's own."""
    await session.commit()


async def test_change_fund_outside_journal(client, session, auth):
    """An imported journal: Change fund voids it, writing replaced_by, and posts {je_id}:fund:1
    keeping origin; a manual journal is refused with the void-or-reverse message."""
    f = await _setup(client, auth)
    je = await _outside_journal(client, auth, [("1111", 50, 0), ("4100", 0, 50)], "unrestricted")
    r = await _change_journal(client, session, auth, je, all=f["U1"])
    assert r.status_code == 200, r.text
    voided = await _state(session, auth, je)
    copy = await _state(session, auth, f"{je}:fund:1")
    assert voided["status"] == "void" and copy["status"] == "posted"
    assert copy.get("origin") == "file_import" and _funds_of(copy["entries"]) == {f["U1"]}
    voids = [e for e in await _ledger(session, auth, je) if e.event_type == "acc.journal_entry.voided"
             and e.entity_id == je]
    assert voids[-1].data["replaced_by"] == f"{je}:fund:1"

    manual = await _emit(session, auth, [("1111", 20, 0, None), ("4100", 0, 20, None)])
    await session.commit()
    r = await _change_journal(client, session, auth, manual, all=f["U1"])
    assert r.status_code == 422 and _key(r) == "posting.fund.change_not_outside", r.text


async def test_change_fund_backing_line_refused(client, session, auth):
    """An outside unrestricted cost backing a standing release cannot move onto R until the
    release is reversed."""
    f = await _setup(client, auth)
    await _emit(session, auth, [("1111", 500, 0, f["R"]), ("4100", 0, 500, f["R"])])
    await _emit(session, auth, [("1111", 500, 0, None), ("4100", 0, 500, None)])
    await session.commit()
    cost = await _outside_journal(client, auth, [("6200", 60, 0), ("1111", 0, 60)], "unrestricted")
    release = await client.post("/accounting/funds/release", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "date": "2026-03-02", "reason": "Board minute 12", "fund": f["R"],
        "to_fund": None, "backing": [{"je_id": cost, "line_index": 0, "amount": "60"}]})
    assert release.status_code == 200, release.text
    r = await _change_journal(client, session, auth, cost, all=f["R"])
    assert r.status_code == 422 and _key(r) == "posting.fund.change_backing", r.text
    back = await client.post(f"/accounting/funds/movements/{release.json()['je_id']}/reverse", headers=auth["headers"],
                             json={"date": "2026-03-02", "reason": "Wrong bill"})
    assert back.status_code == 200, back.text
    r = await _change_journal(client, session, auth, cost, all=f["R"])
    assert r.status_code == 200, r.text


# ── the source void stays as it is ─────────────────────────────────────────────────────

@pytest.mark.parametrize("funds", [True, False])
async def test_source_void_unchanged_in_every_company(client, session, auth, funds):
    """Voiding then unvoiding an invoice writes the events the void and unvoid always wrote:
    one batch-numbered void per live recognition entry with its key and metadata, then the
    restore at {root}:unvoid:1 copying the voided lines; a second cycle numbers batch 2."""
    if funds:
        await _setup(client, auth)
    item = await _item(client, auth, "Golden")
    doc = (await client.post("/docs", headers=auth["headers"], json={
        "doc_type": "invoice", "ref_id": f"INV-{uuid.uuid4().hex[:6]}", "line_items": [_line(item, "Golden", 25)],
        "total": 25})).json()["id"]
    from test_credit_note_settlement_owned import _post

    assert (await _post(client, auth, f"/docs/{doc}/finalize")).status_code == 200
    original = await _state(session, auth, f"je:auto:{doc}:fin")
    for cycle in (1, 2):
        assert (await _post(client, auth, f"/docs/{doc}/void", {"reason": "golden"})).status_code == 200
        assert (await _post(client, auth, f"/docs/{doc}/unvoid")).status_code == 200
        events = [(e.entity_id.removeprefix(f"je:auto:{doc}:"), e.event_type, e.idempotency_key,
                   {k: v for k, v in (e.metadata_ or {}).items() if k in ("trigger", "doc_id", "void_batch")})
                  for e in await _ledger(session, auth, f"je:auto:{doc}:")
                  if e.event_type == "acc.journal_entry.voided"]
        live = "fin" if cycle == 1 else "fin:unvoid:1"
        assert events[-1] == (live, "acc.journal_entry.voided", f"je:{doc}:voided:{cycle}:{live}:void",
                              {"trigger": "doc.voided", "doc_id": doc, "void_batch": cycle}), events
        assert "replaced_by" not in (await _ledger(session, auth, f"je:auto:{doc}:{live}"))[-1].data
        restored = await _state(session, auth, f"je:auto:{doc}:fin:unvoid:{cycle}")
        assert restored["status"] == "posted" and _shape(restored["entries"]) == _shape(original["entries"])


# ── what undoes a changed record ───────────────────────────────────────────────────────

async def _standing(session, auth, prefix: str) -> dict:
    """(fund, account) -> what the posted entries under ``prefix`` hold, zeros left out."""
    session.expire_all()
    out: dict = {}
    for row in (await session.scalars(select(Projection).where(
            Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry",
            Projection.entity_id.startswith(prefix, autoescape=True)))).all():
        if (row.state or {}).get("status") != "posted":
            continue
        for e in row.state.get("entries") or []:
            key = (e.get("fund_id"), e["account"])
            out[key] = out.get(key, Decimal(0)) + Decimal(str(e.get("debit") or 0)) - Decimal(str(e.get("credit") or 0))
    return {k: v for k, v in out.items() if v}


async def _outside_bill(client, auth, fund: str, amount=30) -> str:
    """An open bill for supplies brought in by the document import on ``fund``; it posts on import."""
    doc = f"doc:IMP-{uuid.uuid4().hex[:8]}"
    record = _issued_import(doc, [{**_line(None, "Supplies", amount), "receive_as": "expense"}])
    record["data"].update(doc_type="bill", contact_id="supplier:1")
    r = await client.post("/docs/import/batch", headers=auth["headers"], json={
        "records": [record], "fund_choices": {"records": {doc: fund}}})
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    return doc


@pytest.mark.parametrize("kind", ["invoice", "bill"])
async def test_change_fund_then_undo_leaves_every_fund_zero(client, session, auth, kind):
    """Post, Change fund, then the source void: the record's own entries leave every fund at
    zero on every account they touch, the copy voided with them; unvoid restores the changed
    fund, not the one the record was posted on."""
    from test_credit_note_settlement_owned import _post

    f = await _setup(client, auth)
    if kind == "invoice":
        doc, before, after = await _outside_invoice(client, auth, f["R"]), f["R"], None
        r = await _change_doc(client, session, auth, doc, document="unrestricted")
    else:
        doc, before, after = await _outside_bill(client, auth, "unrestricted"), None, f["U1"]
        r = await _change_doc(client, session, auth, doc, document=f["U1"])
    assert r.status_code == 200, r.text
    prefix = f"je:auto:{doc}:"
    changed = await _standing(session, auth, prefix)
    assert changed and {fund for fund, _ in changed} == {after}, changed
    assert before not in {fund for fund, _ in changed}

    assert (await _post(client, auth, f"/docs/{doc}/void", {"reason": "Raised in error"})).status_code == 200
    assert await _standing(session, auth, prefix) == {}
    assert (await _post(client, auth, f"/docs/{doc}/unvoid")).status_code == 200
    assert await _standing(session, auth, prefix) == changed


async def test_change_fund_opening_held_document_refused(client, session, auth):
    """A purchase order imported into books whose opening balances hold it, billed since:
    Change fund is refused with the opening-balance message, nothing written; a document the
    opening balances do not hold changes."""
    from test_cost_restatement import _item as _lot_item
    from test_credit_note_settlement_owned import _post
    from test_imported_document_cutover import _import
    from test_outside_lines import _choose
    from test_receipt_accounting import _OPENING

    f = await _setup(client, auth)
    lot = await _lot_item(client, auth, _OPENING, qty=10)
    po = await _import(client, auth, lot, qty=10, received=4)
    r = await _post(client, auth, f"/docs/{po}/finalize")
    if r.status_code == 409 and _key(r) == "posting.fund.choice_needed":
        assert (await _choose(client, auth, po, document="unrestricted")).status_code == 200
        r = await _post(client, auth, f"/docs/{po}/finalize")
    assert r.status_code == 200, r.text
    held = await _standing(session, auth, f"je:auto:{po}:")
    assert held, "the bill posted what the opening balances do not hold"
    r = await _change_doc(client, session, auth, po, document=f["U1"])
    assert r.status_code == 422 and _key(r) == "posting.fund.change_opening_held", r.text
    assert await _standing(session, auth, f"je:auto:{po}:") == held

    doc = await _outside_invoice(client, auth, f["R"])
    assert (await _change_doc(client, session, auth, doc, document="unrestricted")).status_code == 200
