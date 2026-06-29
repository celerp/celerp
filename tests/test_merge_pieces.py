# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for https://github.com/celerp/celerp/issues/197.

Merging stocked items must handle the additive `pieces` count correctly:

1. **Type-insensitive** — `pieces` may be stored as `int` / `float` / numeric `str`
   (different write paths produce different types). The merge must coerce to a number
   before comparing, so two items holding the same count merge cleanly.
2. **Summed** — `pieces` is additive: if **every** source has `pieces` set, the merged
   item's `pieces` is the **sum**; if **at least one** source has it unset, the merged
   item has **no `pieces`** (an unknowable total must not be guessed).

These are the six scenarios documented in `data/bug12.md`. They FAIL against the current
merge (which compares raw `str(v)` and never sums) and pass once the merge coerces and sums.
"""

from __future__ import annotations

import uuid

import pytest

_UNSET = object()  # sentinel: source has no `pieces` attribute at all


async def _token(client) -> str:
    email = f"gems-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Gems & Jewelry", "email": email, "name": "Owner", "password": "pw"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def _seed(client, headers, sku: str, pieces, *, qty: float = 32.0) -> str:
    """Create a carat-sold stone with `attributes.pieces` of the given value/type.

    `pieces is _UNSET` leaves the attribute off entirely (the "unset" case).
    """
    payload: dict = {
        "sku": sku,
        "name": sku,
        "quantity": qty,
        "sell_by": "carat",
        "category": "colored_stone",
    }
    if pieces is not _UNSET:
        payload["attributes"] = {"pieces": pieces}
    r = await client.post("/items", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _merge(client, headers, source_ids: list[str], target_id: str) -> str:
    r = await client.post(
        "/items/merge",
        json={"source_entity_ids": source_ids, "target_sku_from": target_id},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _read_pieces(client, headers, item_id: str):
    """Return the merged item's `pieces` (GET flattens attributes → top-level)."""
    r = await client.get(f"/items/{item_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json().get("pieces")


def _assert_pieces(actual, expected) -> None:
    if expected is None:
        assert actual in (None, ""), f"expected no pieces, got {actual!r}"
    else:
        assert actual is not None and int(float(actual)) == expected, (
            f"expected pieces == {expected}, got {actual!r}"
        )


# (name, source A pieces, source B pieces, expected merged pieces) — see data/bug12.md
_SCENARIOS = [
    ("scenario1_str_str_same",   "5",  "5",    10),   # same value, both str  -> sum
    ("scenario2_int_float_same",  16,  16.0,   32),   # same value, int vs float -> coerce, then sum
    ("scenario3_int_int_same",     8,   8,     16),   # same value, both int   -> sum
    ("scenario4_str_str_diff",   "6",  "4",    10),   # different values, str  -> sum
    ("scenario5_int_int_diff",    16,   8,     24),   # different values, int  -> sum
    ("scenario6_set_unset",      "5", _UNSET,  None), # one set, one unset     -> no pieces
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, a_pieces, b_pieces, expected",
    _SCENARIOS,
    ids=[s[0] for s in _SCENARIOS],
)
async def test_merge_pieces(client, name, a_pieces, b_pieces, expected) -> None:
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    a = await _seed(client, headers, "MERGE-A", a_pieces)
    b = await _seed(client, headers, "MERGE-B", b_pieces)

    merged_id = await _merge(client, headers, [a, b], a)
    merged_pieces = await _read_pieces(client, headers, merged_id)

    _assert_pieces(merged_pieces, expected)