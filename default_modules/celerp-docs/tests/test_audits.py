# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Inventory audit on the unified list lifecycle (list_type=audit): a draft manifest is built/seeded,
Finalize freezes the on-hand snapshot and opens the counting stage, scanning records presence, counts
are entered, then a reversible stock adjustment overwrites item qty to the count and posts a
shrinkage/overage JE. See context/2026-0617-unified-lists-lifecycle-plan.md."""
from __future__ import annotations

import uuid

import pytest


async def _register(client) -> str:
    addr = f"admin-{uuid.uuid4().hex[:8]}@audit.test"
    r = await client.post("/auth/register", json={"company_name": "Audit Co", "email": addr, "name": "A", "password": "pw"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _h(t):
    return {"Authorization": f"Bearer {t}"}


async def _location(client, t, name="Warehouse A") -> str:
    r = await client.post("/companies/me/locations", headers=_h(t), json={"name": name, "type": "warehouse"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _item(client, t, sku, *, loc, qty, barcode=None, cost_total=None, inventory_type="stocked") -> str:
    body = {"status": "available", "sku": sku, "name": sku, "quantity": qty, "sell_by": "piece", "location_id": loc,
            "inventory_type": inventory_type}
    if barcode:
        body["barcode"] = barcode
    if cost_total is not None:
        body["cost_total"] = cost_total
    r = await client.post("/items", headers=_h(t), json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _audit(client, t, loc) -> dict:
    r = await client.post("/lists/audit", headers=_h(t), json={"location_id": loc})
    assert r.status_code == 200, r.text
    return r.json()


async def _state(client, t, audit_id) -> dict:
    return (await client.get(f"/lists/{audit_id}", headers=_h(t))).json()


async def _finalize(client, t, audit_id):
    r = await client.post(f"/lists/{audit_id}/finalize", headers=_h(t))
    assert r.status_code == 200, r.text
    return r


# --- creation + pre-population (draft manifest) ----------------------------

@pytest.mark.asyncio
async def test_create_audit_prepopulates_location_as_draft(client):
    t = await _register(client)
    loc_a = await _location(client, t, "A")
    loc_b = await _location(client, t, "B")
    await _item(client, t, "HERE-1", loc=loc_a, qty=5, barcode="1001")
    await _item(client, t, "THERE-1", loc=loc_b, qty=2)                 # other location
    await _item(client, t, "SVC-1", loc=loc_a, qty=0, inventory_type="service")  # non-stock
    audit = await _audit(client, t, loc_a)
    state = await _state(client, t, audit["id"])
    assert state["list_type"] == "audit" and state["location_id"] == loc_a
    assert state["status"] == "draft"  # born as a reviewable draft manifest, not mid-count
    assert {l["sku"] for l in state["line_items"]} == {"HERE-1"}  # only in-location, physical, available
    assert state["ref_id"].startswith("AUD-")
    # No on-hand snapshot yet — that is frozen at Finalize.
    assert "on_hand" not in state["line_items"][0]


@pytest.mark.asyncio
async def test_finalize_freezes_onhand_and_opens_counting(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "F1", loc=loc, qty=9, cost_total=90, barcode="2001")
    audit = (await _audit(client, t, loc))["id"]
    # Counting is blocked while still a draft.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 7})).status_code == 409
    await _finalize(client, t, audit)
    state = await _state(client, t, audit)
    assert state["status"] == "finalized"
    assert state["line_items"][0]["on_hand"] == 9.0  # snapshot frozen at finalize
    # Now counts are accepted.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 7})).status_code == 200


# --- scan dispatch on (status) --------------------------------------------

@pytest.mark.asyncio
async def test_scan_adds_in_draft_records_presence_when_finalized(client):
    t = await _register(client)
    loc = await _location(client, t)
    exp = await _item(client, t, "EXP-1", loc=loc, qty=3, barcode="1002")
    unexp = await _item(client, t, "UNEXP-1", loc=loc, qty=1, barcode="1003")
    loc2 = await _location(client, t, "B")
    await client.post(f"/items/{unexp}/transfer", headers=_h(t), json={"to_location_id": loc2})
    audit = (await _audit(client, t, loc))["id"]
    assert {l["sku"] for l in (await _state(client, t, audit))["line_items"]} == {"EXP-1"}

    # DRAFT scan: ADD a not-yet-on-manifest item; no status change (GDR 2d).
    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1003"})
    assert r.status_code == 200 and r.json()["scanned"] == 1 and r.json()["results"][0]["state"] == "added"
    state = await _state(client, t, audit)
    assert state["status"] == "draft"  # scanning never finalizes
    assert state["line_items"][0]["sku"] == "UNEXP-1"
    assert state["line_items"][0].get("audited_at") is None  # presence is a counting-stage concept

    await _finalize(client, t, audit)
    # FINALIZED scan: the manifest is LOCKED — scanning only checks off items already on the list
    # (records presence, top-insert). It never adds.
    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1002"})
    assert r.status_code == 200 and r.json()["results"][0]["state"] == "audited"
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None
    # Re-scanning the same item just re-confirms it (no error) — you can re-check anything.
    assert (await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1002"})).json()["scanned"] == 1
    # Scanning an item NOT on the locked manifest is reported as failed (a clear reason), not added.
    await _item(client, t, "EXTRA-1", loc=loc, qty=2, barcode="1099")
    rej = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "1099"})
    assert rej.status_code == 200 and rej.json()["scanned"] == 0
    assert "not on this audit" in rej.json()["failed"][0]["label"].lower()
    assert "EXTRA-1" not in {l["sku"] for l in (await _state(client, t, audit))["line_items"]}  # not added
    # Unknown barcode -> reported as failed in any state, never aborts the submit.
    nf = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "NOPE"})
    assert nf.status_code == 200 and [f["code"] for f in nf.json()["failed"]] == ["NOPE"]


@pytest.mark.asyncio
async def test_scan_batch_adds_every_code_in_one_submit_and_reports_failures(client):
    """The scan bar accumulates codes client-side and submits the whole run as one comma-separated
    batch. Every resolvable code is added against a single line_items write; an unresolvable code is
    collected in `failed` and never aborts the good ones."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "AAA", loc=loc, qty=1, barcode="900001")
    b = await _item(client, t, "BBB", loc=loc, qty=1, barcode="900002")
    # Empty the audited location so the manifest starts blank and the adds are unambiguous.
    loc2 = await _location(client, t, "B")
    await client.post(f"/items/{a}/transfer", headers=_h(t), json={"to_location_id": loc2})
    await client.post(f"/items/{b}/transfer", headers=_h(t), json={"to_location_id": loc2})
    audit = (await _audit(client, t, loc))["id"]
    assert (await _state(client, t, audit))["line_items"] == []

    # ONE request carrying three codes: two resolve, one is unknown.
    r = await client.post(f"/lists/{audit}/scan", headers=_h(t),
                          json={"barcode": "900001,900002,NOPE"})
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 2
    assert [f["code"] for f in body["failed"]] == ["NOPE"]
    # Both good codes landed in a single submit; the bad one did not abort them.
    assert {l["sku"] for l in (await _state(client, t, audit))["line_items"]} == {"AAA", "BBB"}


@pytest.mark.asyncio
async def test_scan_batch_rejects_oversized_submit(client):
    """One submit carries its whole accumulated comma list, so an unbounded paste is bounded at the
    route: past MAX_SCAN_BATCH codes the whole submit is refused with 422 before any line is written,
    rather than building an arbitrarily large in-memory set behind one write."""
    from celerp_docs.routes import MAX_SCAN_BATCH
    t = await _register(client)
    loc = await _location(client, t)
    audit = (await _audit(client, t, loc))["id"]
    codes = ",".join(f"X{i}" for i in range(MAX_SCAN_BATCH + 1))
    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": codes})
    assert r.status_code == 422
    # Nothing was written: the batch is refused whole, not partially applied.
    assert (await _state(client, t, audit))["line_items"] == []


async def _quotation(client, t) -> str:
    r = await client.post("/lists", headers=_h(t), json={"list_type": "quotation"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.asyncio
async def test_scan_batch_resolves_in_one_inventory_load(client, monkeypatch):
    """A batch of N codes resolves against ONE inventory load, not one per code: scanning routes the
    whole run through the batch resolver and never the per-code resolver (the N+1 that a 200-code run
    would otherwise trigger). Barcode precedence and ambiguity are unchanged - the batch resolver is
    the same rule, loaded once."""
    import celerp_inventory.routes as inv
    calls = {"batch": 0, "single": 0}
    real_batch, real_single = inv.resolve_items_by_codes, inv.resolve_item_by_code

    async def _batch(session, company_id, codes):
        calls["batch"] += 1
        return await real_batch(session, company_id, codes)

    async def _single(session, company_id, code):
        calls["single"] += 1
        return await real_single(session, company_id, code)

    monkeypatch.setattr(inv, "resolve_items_by_codes", _batch)
    monkeypatch.setattr(inv, "resolve_item_by_code", _single)

    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "Q1", loc=loc, qty=5, barcode="700001")
    await _item(client, t, "Q2", loc=loc, qty=5, barcode="700002")
    await _item(client, t, "Q3", loc=loc, qty=5, barcode="700003")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t),
                          json={"barcode": "700001,700002,700003"})
    assert r.status_code == 200 and r.json()["scanned"] == 3
    assert {l["sku"] for l in (await _state(client, t, q))["line_items"]} == {"Q1", "Q2", "Q3"}
    # ONE load for the whole run; the per-code resolver is never used by scanning.
    assert calls["batch"] == 1
    assert calls["single"] == 0


@pytest.mark.asyncio
async def test_scan_duplicate_barcode_is_reported_not_silently_picked(client, monkeypatch):
    """A barcode present on more than one lot - only reachable for legacy data predating the
    per-company barcode unique index - is reported with the single duplicate-barcode message and
    never silently checked off to one arbitrary lot. The batch resolver is patched to surface the
    duplicate for one code; every other code resolves normally."""
    import celerp_inventory.routes as inv
    from celerp_inventory.routes import ResolveResult, resolve_items_by_codes as _real

    async def _dup(session, company_id, codes):
        base = await _real(session, company_id, codes)
        got = base.get("880001")
        if got is not None and got.kind == "barcode":
            base["880001"] = ResolveResult("barcode", list(got.matches) * 2)  # legacy: same barcode, two lots
        return base

    monkeypatch.setattr(inv, "resolve_items_by_codes", _dup)

    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "DUP1", loc=loc, qty=5, barcode="880001")
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": "880001"})
    assert r.status_code == 200
    body = r.json()
    assert body["scanned"] == 0  # never silently picks a lot
    fail = body["failed"][0]
    assert fail["code"] == "880001"
    assert fail["reason"] == "duplicate_barcode"
    assert fail["label"] == "Duplicate barcode '880001' exists on multiple inventory items"
    assert (await _state(client, t, q))["line_items"] == []  # nothing appended


def test_scan_run_fingerprint_is_canonical_json_no_separator_collision():
    """The run fingerprint is canonical JSON, so a code that itself contains the old join separator
    cannot collide with a two-code batch - a collision a newline-joined payload silently produced."""
    from celerp_docs.routes import _scan_run_fingerprint

    assert _scan_run_fingerprint(["a\nb"], None) != _scan_run_fingerprint(["a", "b"], None)
    assert _scan_run_fingerprint(["x"], None) == _scan_run_fingerprint(["x"], None)  # stable for the same batch


@pytest.mark.asyncio
async def test_scan_accepts_code_longer_than_legacy_64_char_cap(client):
    """The scanner code-length cap shares the inventory scan-code ceiling (max sku/barcode length),
    so a long but valid SKU is scannable rather than rejected by an out-of-date 64-character cap."""
    t = await _register(client)
    loc = await _location(client, t)
    long_sku = "L" + "0" * 120  # 121 chars: within MAX_SKU_LEN, well over the retired 64 cap
    await _item(client, t, long_sku, loc=loc, qty=5)
    q = await _quotation(client, t)

    r = await client.post(f"/lists/{q}/scan", headers=_h(t), json={"barcode": long_sku})
    assert r.status_code == 200, r.text  # not a 422 length rejection
    assert r.json()["scanned"] == 1
    assert [l["sku"] for l in (await _state(client, t, q))["line_items"]] == [long_sku]


@pytest.mark.asyncio
async def test_scan_retry_with_same_run_key_does_not_duplicate(client):
    """The post-commit duplication guard: a quotation scan APPENDS a line per code. If the response (or
    the client's tbody refresh) is lost after the write commits, the operator retries the same run. The
    run_key makes that retry replay the recorded outcome instead of appending the same lines again."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "R1", loc=loc, qty=5, barcode="710001")
    await _item(client, t, "R2", loc=loc, qty=5, barcode="710002")
    q = await _quotation(client, t)

    body = {"barcode": "710001,710002", "run_key": "run-abc"}
    r1 = await client.post(f"/lists/{q}/scan", headers=_h(t), json=body)
    assert r1.status_code == 200 and r1.json()["scanned"] == 2
    assert len((await _state(client, t, q))["line_items"]) == 2

    # Retry of the SAME run (same key): replayed as a duplicate, no second append.
    r2 = await client.post(f"/lists/{q}/scan", headers=_h(t), json=body)
    assert r2.status_code == 200
    assert r2.json().get("duplicate") is True
    assert r2.json()["scanned"] == 2
    assert len((await _state(client, t, q))["line_items"]) == 2

    # A genuinely new run (different key) still appends normally.
    r3 = await client.post(f"/lists/{q}/scan", headers=_h(t),
                           json={"barcode": "710001", "run_key": "run-xyz"})
    assert r3.status_code == 200 and r3.json()["scanned"] == 1
    assert len((await _state(client, t, q))["line_items"]) == 3


@pytest.mark.asyncio
async def test_scan_same_run_key_different_batch_is_rejected(client):
    """The run_key is bound to the batch it acknowledged. If the first response is lost, the operator
    could edit the still-active field to a NEW batch and resubmit under the same key. Replaying the old
    run would silently drop the new batch, so a key reused for a different batch is a 409, not a replay."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "B1", loc=loc, qty=5, barcode="720001")
    await _item(client, t, "B2", loc=loc, qty=5, barcode="720002")
    q = await _quotation(client, t)

    r1 = await client.post(f"/lists/{q}/scan", headers=_h(t),
                           json={"barcode": "720001", "run_key": "k1"})
    assert r1.status_code == 200 and r1.json()["scanned"] == 1

    # Same key, DIFFERENT codes -> rejected, and the new batch is not applied.
    r2 = await client.post(f"/lists/{q}/scan", headers=_h(t),
                           json={"barcode": "720002", "run_key": "k1"})
    assert r2.status_code == 409
    assert r2.json()["code"] == "scan_run_conflict"  # structured, so the scan bar branches on the code
    assert len((await _state(client, t, q))["line_items"]) == 1


@pytest.mark.asyncio
async def test_scan_same_run_key_different_price_list_is_rejected(client):
    """The fingerprint covers the price list too: the same codes priced against a different list is a
    different batch (different money), so the same key with a changed price_list is a 409, not a replay."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "P1", loc=loc, qty=5, barcode="730001")
    q = await _quotation(client, t)

    r1 = await client.post(f"/lists/{q}/scan", headers=_h(t),
                           json={"barcode": "730001", "run_key": "k2", "price_list": "Retail"})
    assert r1.status_code == 200 and r1.json()["scanned"] == 1

    r2 = await client.post(f"/lists/{q}/scan", headers=_h(t),
                           json={"barcode": "730001", "run_key": "k2", "price_list": "Wholesale"})
    assert r2.status_code == 409
    assert r2.json()["code"] == "scan_run_conflict"
    assert len((await _state(client, t, q))["line_items"]) == 1


@pytest.mark.asyncio
async def test_scan_bounds_code_and_key_length(client):
    """Persisted replay data is bounded: an over-long individual code or run_key is refused before any
    line or scan_runs record is written, so a mixed good/oversized batch can never bloat the projection."""
    from celerp_docs.routes import MAX_CODE_LEN, MAX_RUN_KEY_LEN
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "L1", loc=loc, qty=5, barcode="740001")
    q = await _quotation(client, t)

    # An over-long single code is refused (422), nothing written.
    long_code = "7" * (MAX_CODE_LEN + 1)
    r = await client.post(f"/lists/{q}/scan", headers=_h(t),
                          json={"barcode": f"740001,{long_code}", "run_key": "k3"})
    assert r.status_code == 422
    assert (await _state(client, t, q))["line_items"] == []

    # An over-long run_key is refused at the schema boundary (422).
    r = await client.post(f"/lists/{q}/scan", headers=_h(t),
                          json={"barcode": "740001", "run_key": "x" * (MAX_RUN_KEY_LEN + 1)})
    assert r.status_code == 422


# --- duplicate-sku, distinct-lot audit invariant (2026-06-17 sku/batch plan §7.1) --

@pytest.mark.asyncio
async def test_audit_duplicate_sku_distinct_lots_survive_and_scan_binds_lot(client):
    """The single biggest audit-side footgun of non-unique SKU: two same-sku,
    distinct-item lots must stay TWO audit lines keyed on item_id (never merged by
    sku). A scanned barcode checks off exactly its lot; the shared sku is ambiguous
    (409), never a silent check-off; and Adjust emits two independent quantity
    changes (no double-count, no loss)."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "SH", loc=loc, qty=5, cost_total=50, barcode="3501")
    b = await _item(client, t, "SH", loc=loc, qty=7, cost_total=70, barcode="3502")
    audit = (await _audit(client, t, loc))["id"]

    # Draft manifest: two distinct lines, same sku, keyed on item_id.
    lines = (await _state(client, t, audit))["line_items"]
    assert len([l for l in lines if l["sku"] == "SH"]) == 2
    assert {l["item_id"] for l in lines} == {a, b}

    await _finalize(client, t, audit)
    fin_lines = (await _state(client, t, audit))["line_items"]
    # STILL two lines after finalize (dedup keys on item_id, not sku).
    assert {l["item_id"] for l in fin_lines} == {a, b}
    on_hand = {l["item_id"]: l["on_hand"] for l in fin_lines}
    assert on_hand[a] == 5.0 and on_hand[b] == 7.0  # each froze its own on-hand

    # Scan lot B's barcode -> only B checked off, A untouched.
    assert (await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "3502"})).json()["scanned"] == 1
    audited = {l["item_id"]: l.get("audited_at") for l in (await _state(client, t, audit))["line_items"]}
    assert audited[b] is not None and audited[a] is None

    # Scanning/typing the shared SKU is ambiguous -> reported as failed, never a silent check-off.
    rej = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "SH"})
    assert rej.status_code == 200 and rej.json()["scanned"] == 0
    assert rej.json()["failed"][0]["reason"] == "ambiguous_sku"
    assert "sku" in rej.json()["failed"][0]["label"].lower()

    # Count each lot independently, then Adjust: two independent quantity changes.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 4})).status_code == 200
    assert (await client.patch(f"/lists/{audit}/line/{b}", headers=_h(t), json={"counted_qty": 9})).status_code == 200
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    assert r.json()["adjusted"] == 2  # both lots adjusted, neither dropped nor merged
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 4
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 9


# --- count -> adjust (vs LIVE qty) -> undo + JE ----------------------------

@pytest.mark.asyncio
async def test_adjust_overwrites_to_count_posts_je_and_is_undoable(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "A1", loc=loc, qty=10, cost_total=100, barcode="1004")  # unit cost 10
    b = await _item(client, t, "B1", loc=loc, qty=4, cost_total=40, barcode="1005")    # unit cost 10
    c = await _item(client, t, "C1", loc=loc, qty=7, cost_total=70, barcode="1006")    # untouched
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)

    # Count A down to 8 (shrink 2 -> 20), B up to 6 (overage 2 -> 20). Leave C uncounted.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    assert (await client.patch(f"/lists/{audit}/line/{b}", headers=_h(t), json={"counted_qty": 6})).status_code == 200

    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["adjusted"] == 2 and body["shrinkage_value"] == 20.0 and body["overage_value"] == 20.0
    assert body["skipped"] == 1  # C reported as skipped (uncounted), item untouched

    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 8  # new_qty == count
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 6
    assert (await client.get(f"/items/{c}", headers=_h(t))).json()["quantity"] == 7
    st = await _state(client, t, audit)
    assert st["status"] == "closed" and st["result"] == "stock_adjusted"

    # Balanced JE on the shrinkage/overage/inventory accounts.
    ledger = (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
    je = next(e for e in ledger if audit in (e["data"].get("memo") or ""))
    entries = je["data"]["entries"]
    assert abs(sum(float(x.get("debit", 0) or 0) for x in entries) - sum(float(x.get("credit", 0) or 0) for x in entries)) < 1e-6
    assert {"6970", "1130-P", "4300"} <= {x["account"] for x in entries}

    # Undo: quantities restored, audit reopened to finalized, JE voided.
    assert (await client.post(f"/lists/{audit}/undo-adjust", headers=_h(t))).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 10
    assert (await client.get(f"/items/{b}", headers=_h(t))).json()["quantity"] == 4
    st = await _state(client, t, audit)
    assert st["status"] == "finalized" and "result" not in st
    je_events = [e for e in (await client.get("/ledger?entity_type=journal_entry", headers=_h(t))).json()["items"]
                 if audit in (e.get("entity_id") or "")]
    assert any(e.get("event_type") == "acc.journal_entry.voided" for e in je_events)


@pytest.mark.asyncio
async def test_adjust_delta_uses_live_qty_not_frozen_snapshot(client):
    """Decision 5.2: new_qty == count; the JE delta is count - LIVE qty (not count - frozen snapshot).
    A sale between Finalize and Adjust must not be double-counted."""
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "LV1", loc=loc, qty=10, cost_total=100, barcode="3001")  # unit cost 10
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)  # snapshot S = 10
    assert (await _state(client, t, audit))["line_items"][0]["on_hand"] == 10.0
    # A real movement drops live qty to 8 after finalize.
    await client.post(f"/items/{a}/adjust", headers=_h(t), json={"new_qty": 8})
    # Counter physically finds 8.
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 8})).status_code == 200
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200, r.text
    # Overwrite-to-count lands on exactly 8 (not 6 as frozen-delta would give); no value change vs live.
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 8
    assert r.json()["adjusted"] == 0 and r.json()["shrinkage_value"] == 0.0  # count == live -> nothing to post


@pytest.mark.asyncio
async def test_count_zero_sets_quantity_to_zero(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "Z1", loc=loc, qty=5, cost_total=50, barcode="1007")
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)
    assert (await client.patch(f"/lists/{audit}/line/{a}", headers=_h(t), json={"counted_qty": 0})).status_code == 200
    assert (await client.post(f"/lists/{audit}/adjust", headers=_h(t))).status_code == 200
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 0


@pytest.mark.asyncio
async def test_uncounted_lines_skipped(client):
    t = await _register(client)
    loc = await _location(client, t)
    a = await _item(client, t, "S1", loc=loc, qty=5, cost_total=50, barcode="1008")
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)
    # Uncounted -> skipped, item untouched.
    r = await client.post(f"/lists/{audit}/adjust", headers=_h(t))
    assert r.status_code == 200 and r.json()["adjusted"] == 0 and r.json()["skipped"] == 1
    assert (await client.get(f"/items/{a}", headers=_h(t))).json()["quantity"] == 5


@pytest.mark.asyncio
async def test_audit_list_cannot_be_converted(client):
    t = await _register(client)
    loc = await _location(client, t)
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)
    r = await client.post(f"/lists/{audit}/convert", headers=_h(t), json={"target_type": "invoice"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_scanned_highlight_persists_and_clears_via_bulk(client):
    """A scanned line stays marked (audited_at) indefinitely — including across a list-type round
    trip — and is reverted by the bulk Clear scanned action."""
    t = await _register(client)
    loc = await _location(client, t)
    await _item(client, t, "CS1", loc=loc, qty=5, barcode="7001")
    audit = (await _audit(client, t, loc))["id"]
    await _finalize(client, t, audit)

    await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "7001"})
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None

    # Scanned status survives changing the type away and back.
    await client.post(f"/lists/{audit}/change-type", headers=_h(t), json={"list_type": "quotation"})
    await client.post(f"/lists/{audit}/change-type", headers=_h(t), json={"list_type": "audit"})
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None

    # Bulk Clear scanned reverts the highlight.
    r = await client.post(f"/lists/{audit}/set-scanned", headers=_h(t), json={"scanned": False})
    assert r.status_code == 200 and r.json()["changed"] == 1
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is None

    # Bulk Mark as scanned re-applies the highlight (the inverse action).
    r = await client.post(f"/lists/{audit}/set-scanned", headers=_h(t), json={"scanned": True})
    assert r.status_code == 200 and r.json()["changed"] == 1
    assert (await _state(client, t, audit))["line_items"][0]["audited_at"] is not None


@pytest.mark.asyncio
async def test_editable_save_keeps_item_link(client):
    """The editable draft UI sends a line's item id in `entity_id` (no `item_id`). Persisting must
    normalize it to `item_id`, else on-hand freeze (-> 0), scan check-off (-> "not on this audit") and
    dedup all break. Reproduces the regression from making draft audits editable."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "EL1", loc=loc, qty=7, barcode="8001")
    audit = (await _audit(client, t, loc))["id"]

    # Simulate an editable-row autosave: the frontend sends entity_id, not item_id.
    line = (await _state(client, t, audit))["line_items"][0]
    edited = {"entity_id": iid, "sku": "EL1", "name": "EL1", "quantity": 7}  # note: no item_id key
    await client.patch(f"/lists/{audit}", headers=_h(t),
                       json={"fields_changed": {"line_items": {"old": [line], "new": [edited]}}})
    stored = (await _state(client, t, audit))["line_items"][0]
    assert stored.get("item_id") == iid              # normalized on persist (was missing -> the bug)

    await _finalize(client, t, audit)
    assert (await _state(client, t, audit))["line_items"][0]["on_hand"] == 7.0  # freeze found the item

    r = await client.post(f"/lists/{audit}/scan", headers=_h(t), json={"barcode": "8001"})
    assert r.status_code == 200 and r.json()["results"][0]["state"] == "audited"  # check-off works (was 409)


@pytest.mark.asyncio
async def test_finalize_dedupes_duplicate_item_lines(client):
    """An audit counts each item once. If the draft manifest ends up with two lines for the same
    item_id (hand-edited, or via a list-type round trip), Finalize collapses them to one — otherwise
    check-off can never reach the second row (scan always matches the first) and Adjust double-counts."""
    t = await _register(client)
    loc = await _location(client, t)
    iid = await _item(client, t, "DUP-1", loc=loc, qty=4, barcode="9001")
    audit = (await _audit(client, t, loc))["id"]

    # Seeded with one line for the item; inject a second identical line via the editable-save path.
    # A line_items patch pins the current version (the concurrency guard a real editor carries).
    state = await _state(client, t, audit)
    line = state["line_items"][0]
    await client.patch(f"/lists/{audit}", headers=_h(t),
                       json={"expected_version": state["version"],
                             "fields_changed": {"line_items": {"old": [line], "new": [line, dict(line)]}}})
    assert len((await _state(client, t, audit))["line_items"]) == 2  # duplicate present pre-finalize

    await _finalize(client, t, audit)
    lines = (await _state(client, t, audit))["line_items"]
    assert len(lines) == 1                 # collapsed to one line per item_id
    assert lines[0]["item_id"] == iid
    assert lines[0]["on_hand"] == 4.0      # surviving line still froze its on-hand snapshot


@pytest.mark.asyncio
async def test_list_patch_optimistic_version_rejects_stale_writes(client):
    """A draft list carries a version (its latest ledger-entry id), returned by GET. A patch may pin
    expected_version; a patch whose expected_version is not the current version is a stale write
    (another editor moved the list on) and is rejected 409, so concurrent editors cannot silently
    clobber each other. A patch that omits expected_version stays unchecked (backward compatible)."""
    t = await _register(client)
    q = await _quotation(client, t)

    v0 = (await client.get(f"/lists/{q}", headers=_h(t))).json()["version"]
    assert isinstance(v0, int)

    # Correct version -> applied, and the returned version advances.
    r1 = await client.patch(f"/lists/{q}", headers=_h(t),
                            json={"fields_changed": {"customer_name": {"new": "Acme"}},
                                  "expected_version": v0})
    assert r1.status_code == 200
    v1 = r1.json()["version"]
    assert v1 != v0

    # Stale version (v0 again) -> rejected, list unchanged.
    r2 = await client.patch(f"/lists/{q}", headers=_h(t),
                            json={"fields_changed": {"customer_name": {"new": "Evil"}},
                                  "expected_version": v0})
    assert r2.status_code == 409
    assert (await _state(client, t, q))["customer_name"] == "Acme"

    # Fresh version -> applied again.
    r3 = await client.patch(f"/lists/{q}", headers=_h(t),
                            json={"fields_changed": {"customer_name": {"new": "Beta"}},
                                  "expected_version": v1})
    assert r3.status_code == 200

    # No expected_version -> unchecked, still applied.
    r4 = await client.patch(f"/lists/{q}", headers=_h(t),
                            json={"fields_changed": {"customer_name": {"new": "Gamma"}}})
    assert r4.status_code == 200
    assert (await _state(client, t, q))["customer_name"] == "Gamma"


@pytest.mark.asyncio
async def test_list_patch_line_items_replacement_requires_version(client):
    """Replacing line_items is a full-array read-modify-write: a stale editor saving its own array
    would silently drop a concurrent scan's lines. patch_list rejects a line_items replacement that
    carries no expected_version (409, no silent last-write-wins); the same replacement pinned to the
    current version is applied. Scalar-only patches stay version-optional (test above)."""
    t = await _register(client)
    q = await _quotation(client, t)
    v0 = (await client.get(f"/lists/{q}", headers=_h(t))).json()["version"]

    new_lines = [{"item_id": "item:zz", "sku": "ZZ", "quantity": 2}]
    # Replacement without a version pin -> rejected before it can clobber a concurrent write.
    r_missing = await client.patch(f"/lists/{q}", headers=_h(t),
                                   json={"fields_changed": {"line_items": {"new": new_lines}}})
    assert r_missing.status_code == 409
    assert (await _state(client, t, q)).get("line_items") in (None, [])  # nothing written

    # Same replacement pinned to the current version -> applied.
    r_ok = await client.patch(f"/lists/{q}", headers=_h(t),
                              json={"fields_changed": {"line_items": {"new": new_lines}},
                                    "expected_version": v0})
    assert r_ok.status_code == 200, r_ok.text
    assert [li.get("sku") for li in (await _state(client, t, q)).get("line_items") or []] == ["ZZ"]
