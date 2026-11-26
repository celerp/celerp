# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company from before tax codes upgrades without its books moving. Its purchase withholding
row, saved as a negative rate, becomes a withholding code; a bill that withheld that way keeps
what it owes and what it posted, shows the withholding as withheld rather than as negative tax,
and is flagged until its entry is corrected, which posts on a date the user picks and can be
undone. The upgrade tells the company what it changed until an admin dismisses the notice, and
every new tax route checks the user's role and the company the record belongs to."""
from __future__ import annotations

import re

from sqlalchemy import select

from celerp.models.projections import Projection
from tax_legacy_support import ledger_rows, old_bill, old_invoice, old_item, old_shape_company, upgrade
from tax_money_support import (_key, doc, finalized, get, line, ok, pay, post, rebuild_divergence, roles,
                               unbalanced_entries)
from test_company_settings_owners import _user_with_role

_CORRECTIONS = "/settings/taxes/withholding-corrections"


async def _books_hold(session, auth) -> None:
    """Every posted entry balances, the roles sum to zero and a replay of the ledger builds
    exactly the stored projections."""
    assert await unbalanced_entries(session, auth) == []
    assert round(sum((await roles(session, auth)).values()), 2) == 0
    assert await rebuild_divergence(session, auth["company_id"]) == []


async def _journal(session, auth) -> dict:
    session.expire_all()
    rows = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_type == "journal_entry"))).scalars().all()
    return {row.entity_id: row.state for row in rows}


async def _settings(client, auth) -> dict:
    return ok(await client.get("/companies/me", headers=auth["headers"]))["settings"]


def _ref(doc_id: str) -> str:
    return doc_id.split(":", 1)[1]


async def test_legacy_wht_bill_paths_after_upgrade(client, session):
    """An upgraded old-shape bill goes through payment, voided payment, void, unvoid and
    re-finalizing; re-finalizing posts the new shape (input tax in full, the withholding owed to
    the tax office) and clears the flag. Issuing a supplier note against a flagged bill is refused
    with the way to the correction; once corrected it applies at most what the bill owes. The books
    balance and replay to the stored projections after every step."""
    auth = await old_shape_company(session)
    bill = await old_bill(session, auth)
    other = await old_bill(session, auth)
    await upgrade(session, auth)
    await _books_hold(session, auth)

    ok(await pay(client, auth, bill, 1040.0))
    assert (await get(client, auth, bill))["amount_outstanding"] == 0
    await _books_hold(session, auth)

    ok(await post(client, auth, f"/docs/{bill}/void-payment", {"payment_index": 0, "idempotency_key": _key()}))
    assert (await get(client, auth, bill))["amount_outstanding"] == 1040.0
    await _books_hold(session, auth)

    ok(await post(client, auth, f"/docs/{bill}/void"))
    assert (await roles(session, auth))["payable"] == -1040.0  # only the other bill is owed
    await _books_hold(session, auth)

    ok(await post(client, auth, f"/docs/{bill}/unvoid"))
    assert (await roles(session, auth))["payable"] == -2080.0
    await _books_hold(session, auth)

    ok(await post(client, auth, f"/docs/{bill}/revert-to-draft"))
    state = await finalized(client, auth, bill)
    assert not state.get("legacy_wht_entry")
    books = await roles(session, auth)
    assert (books["tax_input"], books["tax_withheld_payable"], books["payable"]) == (110.0, -30.0, -2080.0)
    await _books_hold(session, auth)

    note = {"doc_type": "debit_note", "contact_id": "supplier:1", "original_doc_id": other, "issue_date": "2026-10-01",
            "line_items": [line(1100.0)]}
    draft = ok(await client.post("/docs", headers=auth["headers"], json=note))["id"]
    r = await post(client, auth, f"/docs/{draft}/finalize")
    assert r.status_code == 409
    assert f"Post the withholding correction for bill {_ref(other)} first" in r.text and _CORRECTIONS in r.text
    await _books_hold(session, auth)

    ok(await post(client, auth, f"{_CORRECTIONS}/{other}", {"date": "2026-10-15"}))
    await finalized(client, auth, draft)
    assert (await get(client, auth, other))["amount_outstanding"] == 0  # applied 1040 of the 1100
    await _books_hold(session, auth)


async def test_legacy_wht_correction_entry_and_undo(client, session):
    """The correction for a flagged bill moves its withholding out of input tax into what is owed
    to the tax office, on the date the user picks, and clears the flag; a second try is refused
    and posts nothing. Undo reverses it and flags the bill again. A date in a locked period or
    before the bill is refused."""
    auth = await old_shape_company(session)
    bill = await old_bill(session, auth)
    await upgrade(session, auth)

    listed = ok(await client.get(_CORRECTIONS, headers=auth["headers"]))
    assert [row["doc_id"] for row in listed["bills"]] == [bill]

    before = await roles(session, auth)
    ok(await post(client, auth, f"{_CORRECTIONS}/{bill}", {"date": "2026-10-15"}))
    after = await roles(session, auth)
    assert round(after["tax_input"] - before["tax_input"], 2) == 30.0
    assert round(after["tax_withheld_payable"] - before.get("tax_withheld_payable", 0), 2) == -30.0
    entries = {k: v for k, v in (await _journal(session, auth)).items() if f"{bill}:wht-fix:0" in k}
    assert len(entries) == 1 and next(iter(entries.values()))["entry_date"].startswith("2026-10-15")
    state = await get(client, auth, bill)
    assert not state.get("legacy_wht_entry") and state["withholding_correction"] == next(iter(entries))

    r = await post(client, auth, f"{_CORRECTIONS}/{bill}", {"date": "2026-10-16"})
    assert r.status_code == 422
    assert len([k for k in await _journal(session, auth) if f"{bill}:wht-fix:" in k]) == 1

    ok(await post(client, auth, f"{_CORRECTIONS}/{bill}/undo"))
    assert await roles(session, auth) == before
    state = await get(client, auth, bill)
    assert state["legacy_wht_entry"] is True and "withholding_correction" not in state

    ok(await post(client, auth, "/accounting/period-lock", {"lock_date": "2026-10-31"}))
    assert (await post(client, auth, f"{_CORRECTIONS}/{bill}", {"date": "2026-10-20"})).status_code == 422
    ok(await post(client, auth, "/accounting/period-lock", {"lock_date": None}))
    assert (await post(client, auth, f"{_CORRECTIONS}/{bill}", {"date": "2026-09-01"})).status_code == 422
    ok(await post(client, auth, f"{_CORRECTIONS}/{bill}", {"date": "2026-11-05"}))
    assert any(f"{bill}:wht-fix:1" in k for k in await _journal(session, auth))
    await _books_hold(session, auth)


async def test_migration_negative_rates_to_withholding(client, session):
    """The negative purchase rate becomes a withholding code taken at the document, with a rate
    version of 3 and a migrated id; no negative rate is left. An issued bill now shows tax 70,
    total 1070 and withheld 30, owes the same 1040 and is flagged; a draft is converted without
    the flag; a bill without withholding and every posted entry stay as they were. Running the
    upgrade again writes nothing."""
    from celerp.services.doc_balance import amount_due

    auth = await old_shape_company(session)
    issued = await old_bill(session, auth)
    draft = await old_bill(session, auth, status="draft")
    plain = await old_invoice(session, auth)
    plain_before = await get(client, auth, plain)
    journal_before = await _journal(session, auth)

    await upgrade(session, auth)

    purchase = ok(await client.get("/companies/me/purchasing-taxes", headers=auth["headers"]))
    wht = next(c for c in purchase if c["name"] == "WHT 3%")
    assert (wht["category"], wht["withholding_point"]) == ("withholding", "document")
    assert [v["rate"] for v in wht["versions"]] == [3.0] and re.fullmatch(r"m-[0-9a-f]{12}", wht["id"])
    assert all(v["rate"] >= 0 for c in purchase for v in c["versions"])

    state = await get(client, auth, issued)
    taxes = state["line_items"][0]["taxes"]
    held = next(t for t in taxes if t.get("category") == "withholding")
    assert (held["rate"], held["amount"], held["withholding_point"]) == (3.0, 30.0, "document")
    assert all(t["rate"] >= 0 and t["amount"] >= 0 for t in taxes)
    assert (state["tax"], state["total"], state["withheld"]) == (70.0, 1070.0, 30.0)
    assert state["legacy_wht_entry"] is True
    assert amount_due(state) == 1040.0 == state["amount_outstanding"]

    converted = await get(client, auth, draft)
    assert all(t["rate"] >= 0 for t in converted["line_items"][0]["taxes"])
    assert converted["withheld"] == 30.0 and not converted.get("legacy_wht_entry")
    assert await get(client, auth, plain) == plain_before
    assert await _journal(session, auth) == journal_before

    rows, settings = await ledger_rows(session, auth), await _settings(client, auth)
    await upgrade(session, auth)
    assert await ledger_rows(session, auth) == rows
    assert await _settings(client, auth) == settings
    assert await rebuild_divergence(session, auth["company_id"]) == []


async def test_projection_rebuild_matches_stored(client, session):
    """After the upgrade's events a replay of the ledger builds the stored projections; a
    projection changed behind the ledger's back is reported."""
    from celerp.services.company_lock import lock_company

    auth = await old_shape_company(session)
    bill = await old_bill(session, auth)
    await old_invoice(session, auth)
    await upgrade(session, auth)
    assert await rebuild_divergence(session, auth["company_id"]) == []

    await lock_company(session, auth["company_id"])
    row = (await session.execute(select(Projection).where(
        Projection.company_id == auth["company_id"], Projection.entity_id == bill))).scalar_one()
    row.state = {**row.state, "withheld": 0.0}
    await session.commit()
    assert await rebuild_divergence(session, auth["company_id"]) == [bill]


async def test_upgrade_notice_lists_migration_outcomes(client, session):
    """The upgrade notice counts what needs the company's attention: no country chosen, codes
    no category fits, posting roles it could not map, bills awaiting their withholding
    correction and item tax codes that match no code."""
    auth = await old_shape_company(session, restored_elsewhere=True)
    await old_bill(session, auth)
    await old_item(client, auth, "KNOWN", ["VAT 7%"])
    await old_item(client, auth, "GONE", ["Gone 9%"])
    counts = await upgrade(session, auth)

    expected = {"country_missing": True, "codes_without_category": 1, "unmapped_roles": ["tax_withheld_payable"],
                "bills_awaiting_correction": 1, "unmatched_item_tax_codes": 1}
    assert counts == expected
    settings = await _settings(client, auth)
    assert settings["upgrade_notice"] == expected and not settings.get("upgrade_notice_dismissed")


async def test_upgrade_notice_dismiss_sets_company_flag(client, session):
    """An operator cannot dismiss the notice; an admin's dismiss hides it for every user of the
    company and keeps the counts for the localization page."""
    auth = await old_shape_company(session)
    await old_bill(session, auth)
    counts = await upgrade(session, auth)
    operator = await _user_with_role(client, session, auth["headers"], "operator")

    r = await client.post("/settings/taxes/upgrade-notice/dismiss", headers=operator)
    assert r.status_code == 403
    assert not (await _settings(client, auth)).get("upgrade_notice_dismissed")

    ok(await post(client, auth, "/settings/taxes/upgrade-notice/dismiss"))
    settings = await _settings(client, auth)
    assert settings["upgrade_notice_dismissed"] is True and settings["upgrade_notice"] == counts


_ROUTES = [  # (method, path, minimum role, body)
    ("post", "/settings/taxes/localization/preview", "admin", None),
    ("post", "/settings/taxes/localization/confirm", "admin", {"binding": "x", "idempotency_key": "k"}),
    ("post", "/settings/taxes/localization/undo", "admin", {"operation_id": "op"}),
    ("post", "/settings/taxes/upgrade-notice/dismiss", "admin", None),
    ("get", "/settings/taxes/rate-review/op", "operator", None),
    ("post", "/settings/taxes/rate-review/op/apply", "operator", {"item_ids": [], "binding": "x"}),
    ("post", "/settings/taxes/rate-review/op/undo", "operator", None),
    ("post", "/settings/taxes/cash-basis/preview", "manager", None),
    ("post", "/settings/taxes/cash-basis/confirm", "manager", {"binding": "x", "idempotency_key": "k"}),
    ("get", _CORRECTIONS, "manager", None),
    ("post", f"{_CORRECTIONS}/doc:none", "manager", {"date": "2026-10-15"}),
    ("post", f"{_CORRECTIONS}/doc:none/undo", "manager", None),
]
_BELOW = {"operator": "viewer", "manager": "operator", "admin": "manager"}


async def test_permissions_new_routes(client, session):
    """Each new tax route refuses the role one below its minimum and lets its minimum through;
    another company's operation and bill are not found."""
    auth = await old_shape_company(session)
    users = {role: await _user_with_role(client, session, auth["headers"], role)
             for role in ("viewer", "operator", "manager")}
    users["admin"] = auth["headers"]
    for method, path, role, body in _ROUTES:
        kw = {"json": body} if body is not None else {}
        below = await getattr(client, method)(path, headers=users[_BELOW[role]], **kw)
        assert below.status_code == 403, (path, below.status_code)
        at = await getattr(client, method)(path, headers=users[role], **kw)
        assert at.status_code != 403, (path, at.text)

    other = await old_shape_company(session)
    foreign_bill = await old_bill(session, other)
    await upgrade(session, other)
    r = await client.patch("/companies/me", headers=other["headers"], json={"settings": {"country": "TH"}})
    assert r.status_code == 200, r.text
    preview = ok(await post(client, other, "/settings/taxes/localization/preview"))
    applied = ok(await post(client, other, "/settings/taxes/localization/confirm",
                            {"binding": preview["fingerprint"], "idempotency_key": _key()}))
    r = await client.get(f"/settings/taxes/rate-review/{applied['operation_id']}", headers=auth["headers"])
    assert r.status_code == 404
    r = await post(client, auth, f"{_CORRECTIONS}/{foreign_bill}", {"date": "2026-10-15"})
    assert r.status_code == 404
