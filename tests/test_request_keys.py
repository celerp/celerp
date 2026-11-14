# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A request key names one request.

Initial conditions per test: a fresh company with the seeded chart (1111 the default bank),
funds turned on, two unrestricted funds made through POST /accounting/funds and 500 of cash
in the first, posted by a manual entry. A request sent
again under its key with the same body answers what the first answered and posts nothing more;
the key sent with any field different is refused (409) and nothing is posted.
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio


async def _funds(client, auth) -> tuple[str, str]:
    r = await client.put("/accounting/funds/setting", headers=auth["headers"], json={"enabled": True})
    assert r.status_code == 200, r.text
    ids = []
    for name in ("Board reserve", "Building reserve"):
        r = await client.post("/accounting/funds", headers=auth["headers"], json={
            "request_key": uuid.uuid4().hex, "name": name})
        assert r.status_code == 200, r.text
        ids.append(r.json()["id"])
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-02-01", "memo": "Opening cash", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 500.0, "credit": 0.0, "fund_id": ids[0]},
                    {"account": "3200", "debit": 0.0, "credit": 500.0, "fund_id": ids[0]}]})
    assert r.status_code == 200, r.text
    return ids[0], ids[1]


async def test_a_transfer_key_sent_with_another_amount_is_refused(client, auth):
    """A transfer's request key sent again with a different amount is refused, not answered with
    the first transfer as if the second had posted."""
    a, b = await _funds(client, auth)
    body = {"request_key": uuid.uuid4().hex, "date": "2026-03-01", "reason": "Move", "from_fund": a,
            "to_fund": b, "amount": 10}
    first = await client.post("/accounting/funds/transfer", headers=auth["headers"], json=body)
    assert first.status_code == 200, first.text
    again = await client.post("/accounting/funds/transfer", headers=auth["headers"], json=body)
    assert again.status_code == 200 and again.json() == first.json(), again.text
    other = await client.post("/accounting/funds/transfer", headers=auth["headers"], json={**body, "amount": 25})
    assert other.status_code == 409, other.text


async def _journal_ids(client, auth) -> list[str]:
    r = await client.get("/accounting/journal", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return sorted(e["je_id"] for e in r.json()["entries"])


async def _fund_names(client, auth) -> list:
    r = await client.get("/accounting/funds", headers=auth["headers"])
    assert r.status_code == 200, r.text
    return sorted((f["id"], f["name"]) for f in r.json()["items"])


def _doors(rng, a: str, b: str, je_id: str) -> list[tuple[str, dict, dict]]:
    """(path, body, one changed copy of the body) per keyed fund door; the changed field and its
    new value are drawn from ``rng``."""
    day = f"2026-03-{rng.randint(1, 27):02d}"
    word = f"r{rng.randint(0, 10**6)}"
    amount = rng.randint(1, 40)
    transfer = {"request_key": uuid.uuid4().hex, "date": day, "reason": word, "from_fund": a,
                "to_fund": b, "amount": amount}
    opening = {"request_key": uuid.uuid4().hex, "date": day, "reason": word,
               "assignments": [{"account": "1111", "fund": b, "amount": amount}]}
    fund = {"request_key": uuid.uuid4().hex, "name": f"Fund {word}", "restricted": rng.random() < 0.5,
            "purpose": f"Purpose {word}"}
    reverse = {"date": day, "reason": word}
    other_day = f"2026-03-{(int(day[-2:]) % 27) + 1:02d}"
    changes = {
        "transfer": [("amount", amount + rng.randint(1, 9)), ("date", other_day), ("reason", word + "x"),
                     ("to_fund", None)],
        "opening": [("assignments", [{"account": "1111", "fund": b, "amount": amount + 1}]),
                    ("date", other_day), ("reason", word + "x")],
        "fund": [("name", f"Fund {word}x"), ("restricted", not fund["restricted"]), ("purpose", f"Purpose {word}x")],
        "reverse": [("date", other_day), ("reason", word + "x")],
    }
    bodies = {"transfer": ("/accounting/funds/transfer", transfer), "opening": ("/accounting/funds/opening", opening),
              "fund": ("/accounting/funds", fund), "reverse": (f"/accounting/funds/movements/{je_id}/reverse", reverse)}
    out = []
    for name, (path, body) in bodies.items():
        field, value = rng.choice(changes[name])
        out.append((path, body, {**body, field: value}))
    rng.shuffle(out)
    return out


@pytest.mark.parametrize("seed", range(4))
async def test_every_keyed_fund_door_binds_its_key_to_the_whole_request(client, auth, seed):
    """Seeded oracle over the keyed fund doors (transfer, opening by fund, create fund, reverse a
    movement): the same request again answers what the first did and adds nothing to the books;
    the key with any one field changed is refused 409 and adds nothing either."""
    import random
    rng = random.Random(seed)
    a, b = await _funds(client, auth)
    r = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
        "ts": "2026-02-02", "memo": "Unassigned cash", "idempotency_token": uuid.uuid4().hex,
        "entries": [{"account": "1111", "debit": 100.0, "credit": 0.0},
                    {"account": "3200", "debit": 0.0, "credit": 100.0}]})
    assert r.status_code == 200, r.text
    moved = await client.post("/accounting/funds/transfer", headers=auth["headers"], json={
        "request_key": uuid.uuid4().hex, "date": "2026-02-10", "reason": "To reverse", "from_fund": a,
        "to_fund": b, "amount": 5})
    assert moved.status_code == 200, moved.text
    for path, body, changed in _doors(rng, a, b, moved.json()["je_id"]):
        first = await client.post(path, headers=auth["headers"], json=body)
        assert first.status_code == 200, (path, first.text)
        books, funds = await _journal_ids(client, auth), await _fund_names(client, auth)
        again = await client.post(path, headers=auth["headers"], json=body)
        assert again.status_code == 200 and again.json() == first.json(), (path, again.text)
        other = await client.post(path, headers=auth["headers"], json=changed)
        assert other.status_code == 409, (path, changed, other.text)
        assert other.json()["detail"]["message_key"] == "request.key_reused", other.text
        assert await _journal_ids(client, auth) == books and await _fund_names(client, auth) == funds, path


async def test_a_movement_key_names_its_kind(client, session, auth, ids):
    """A movement's key sent again with the same body for another kind of movement is refused:
    the key names one movement, its kind included, so a reallocation is never answered with the
    transfer posted under its key."""
    from datetime import date
    from fastapi import HTTPException
    from celerp_accounting.funds import post_fund_movement
    a, b = await _funds(client, auth)
    key, body = uuid.uuid4().hex, {"from_fund": a, "to_fund": b, "amount": 10}

    def move(kind):
        return post_fund_movement(session, ids["company_id"], ts=date(2026, 3, 1), reason="Move",
                                  actor=ids["user_id"], request_key=key, request=body, kind=kind,
                                  legs=[(a, b, "1111", 10)])
    first = await move("fund_transfer")
    assert await move("fund_transfer") == first
    with pytest.raises(HTTPException) as refused:
        await move("fund_reallocation")
    assert refused.value.status_code == 409
