# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Generated follow-ups of an online payment, checked against a reference model after every
delivery. Each case is a seed. It refunds part of the 1,070.00 charge, then tells a dispute's
story as Stripe ends one (won, lost, or an inquiry closed with a warning, sometimes followed by
the dispute it escalates to), drops some of its steps, delivers the rest in natural or shuffled
order, loses acknowledgements and resends steps, and sometimes names more than the charge or
another currency, so the dispute cannot be recorded.

The model knows only what the money did: a refund gives its amount back once; a dispute stands
where the steps reported so far put it, whatever their order: lost when it was decided lost,
given back when it was won or the money was reinstated, held in Disputed payments when only its
withdrawal is known, and nothing before that. A dispute that cannot be recorded moves nothing.
The company is told once of each step of each dispute, recorded or not. It never calls the code
it checks.

The fixed seeds always run; DISPUTE_ORACLE_RANDOM more (default 4) are drawn from
DISPUTE_ORACLE_SEED, else the commit (GITHUB_SHA) or the xdist run id, so every runner of one run
draws the same cases, else at random. A failing case names its seed: DISPUTE_ORACLE_SEED=<seed>
with DISPUTE_ORACLE_RANDOM=1 replays it.
"""
from __future__ import annotations

import hashlib
import os
import random
import secrets
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from sqlalchemy import text

from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401
from test_payment_disputes import BANK, DISPUTED, LOSSES, _at, _deliver_dispute, _notices, _paid_invoice
from test_stripe_refunds import _ar_account, _books, _deliver_one, _doc, _payment
from test_system_recovery_exact import _migration_history  # noqa: F401

pytestmark = pytest.mark.asyncio

FIXED_SEEDS = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233]
RANDOM_COUNT = int(os.environ.get("DISPUTE_ORACLE_RANDOM", "4"))
GROSS = 107000
STORIES = {"won": ("created", "funds_withdrawn", "funds_reinstated", ("closed", "won")),
           "lost": ("created", "funds_withdrawn", ("closed", "lost")),
           "inquiry": ("created", ("closed", "warning_closed"))}


def _random_seed(i: int) -> int:
    """The i-th random case's seed, drawn when the case runs so its test id stays random-<i>."""
    base = (os.environ.get("DISPUTE_ORACLE_SEED") or os.environ.get("GITHUB_SHA")
            or os.environ.get("PYTEST_XDIST_TESTRUNUID"))
    if base and base.isdigit() and RANDOM_COUNT == 1:
        return int(base)
    if base is None:
        return secrets.randbelow(10**9)
    return int(hashlib.sha256(f"{base}:{i}".encode()).hexdigest()[:8], 16)


# ── Reference model ───────────────────────────────────────────────────────────


@dataclass
class Dispute:
    amount_minor: int
    currency: str
    steps: set = field(default_factory=set)
    outcome: str | None = None

    @property
    def recordable(self) -> bool:
        return self.currency == "usd" and self.amount_minor <= GROSS

    def position(self) -> dict[str, Decimal]:
        """Net debit per account of what the dispute has moved."""
        amount = Decimal(self.amount_minor) / 100
        if not self.recordable:
            return {}
        if self.outcome == "lost":
            return {LOSSES: amount, BANK: -amount}
        if self.outcome == "won" or "funds_reinstated" in self.steps:
            return {}
        if "funds_withdrawn" in self.steps:
            return {DISPUTED: amount, BANK: -amount}
        return {}


def _story(rng: random.Random, kind: str) -> list[tuple[str, str | None]]:
    steps = [s if isinstance(s, tuple) else (s, None) for s in STORIES[kind]]
    kept = [s for s in steps if rng.random() >= (0.2 if s[0] == "closed" else 0.3)]
    if rng.random() < 0.5:
        rng.shuffle(kept)
    return kept


def _case(rng: random.Random) -> tuple[list[int], list[tuple[str, Dispute, list]]]:
    refunds, left = [], GROSS - 1
    for _ in range(rng.choice((0, 0, 1, 2))):
        amount = rng.randint(1, max(1, left // 2))
        refunds.append(amount)
        left -= amount
    disputes = []
    if rng.random() < 0.3:
        disputes.append(("dp_inq", Dispute(rng.randint(1, GROSS), "usd"), _story(rng, "inquiry")))
    kind = rng.choice(("won", "won", "lost", "lost", "inquiry"))
    amount, currency = rng.randint(1, GROSS), "usd"
    if rng.random() < 0.15:
        amount, currency = rng.choice(((GROSS + rng.randint(1, 5000), "usd"), (amount, "eur")))
    disputes.append(("dp_1", Dispute(amount, currency), _story(rng, kind)))
    return refunds, disputes


async def _dispute_postings(engine, entity_id) -> tuple[dict[str, Decimal], list[str]]:
    """Net debit per account over the invoice's posted dispute entries, and their keys."""
    async with maker(engine)() as s:
        rows = (await s.execute(text("SELECT entity_id, state FROM projections WHERE entity_id LIKE :j"),
                                {"j": f"je:auto:{entity_id}:dispute:%"})).all()
    net: dict[str, Decimal] = {}
    keys = []
    for entity, je in rows:
        if je.get("status") != "posted":
            continue
        keys.append(entity)
        for line in je["entries"]:
            net[line["account"]] = (net.get(line["account"], Decimal(0)) + Decimal(str(line.get("debit") or 0))
                                    - Decimal(str(line.get("credit") or 0)))
    return {k: v for k, v in net.items() if v}, keys


async def _seed_case(seed: int, engine, client, monkeypatch) -> None:
    rng = random.Random(seed)
    refunds, disputes = _case(rng)
    a, invoice, cloud = await _paid_invoice(engine, client, monkeypatch)
    ar = await _ar_account(engine, invoice)

    deliveries: list[tuple[dict, str | None]] = []
    for i, amount in enumerate(refunds):
        cloud.refund(a, invoice, f"re_{i}", amount, _at(1 + i))
        deliveries.append((cloud.deliveries[-1], None))
    for dispute_id, d, steps in disputes:
        for state, outcome in steps:
            cloud.dispute(a, invoice, state, _at(3 + len(deliveries)), outcome=outcome,
                          amount_minor=d.amount_minor, currency=d.currency, dispute_id=dispute_id)
            deliveries.append((cloud.deliveries[-1], dispute_id))

    model = {dispute_id: d for dispute_id, d, _ in disputes}
    refunded, told, trail = 0, 0, []
    for delivery, dispute_id in deliveries:
        # Lost acknowledgements are delivered again; Cloud may also resend a step as a new delivery.
        sends = [delivery] * (2 if rng.random() < 0.3 else 1)
        if rng.random() < 0.2:
            sends.append({**delivery, "delivery_id": str(uuid.uuid4())})
        for send in sends:
            trail.append(f"{send.get('dispute_id') or send.get('refund_id')}:{send.get('state') or 'refund'}")
            deliver = _deliver_dispute if send["kind"] == "dispute" else _deliver_one
            assert await deliver(send), f"seed {seed} not acknowledged after {trail}"
        if dispute_id is None:
            refunded += delivery["amount_minor"]
        else:
            d = model[dispute_id]
            if delivery["state"] not in d.steps:
                told += 1
            d.steps.add(delivery["state"])
            if delivery["outcome"] is not None and d.outcome is None:
                d.outcome = delivery["outcome"]

        expected: dict[str, Decimal] = {}
        for d in model.values():
            for account, amount in d.position().items():
                expected[account] = expected.get(account, Decimal(0)) + amount
        moved, keys = await _dispute_postings(engine, invoice)
        assert moved == {k: v for k, v in expected.items() if v}, f"seed {seed} after {trail}"
        assert len(keys) == len(set(keys)), f"seed {seed} after {trail}: {keys}"
        assert len(await _notices(engine, a)) == told, f"seed {seed} after {trail}"
        left = Decimal(GROSS - refunded) / 100
        doc = await _doc(engine, invoice)
        assert Decimal(str(_payment(doc).get("refunded") or 0)) == Decimal(refunded) / 100, f"seed {seed}"
        assert Decimal(str(doc["amount_paid"])) == left, f"seed {seed} after {trail}"
        assert await _books(engine, invoice) == {BANK: left, ar: -left}, f"seed {seed} after {trail}"


@pytest.mark.parametrize("seed", FIXED_SEEDS)
async def test_dispute_follow_ups_agree_with_the_reference_model(seed, real_engine, real_client, monkeypatch):
    await _seed_case(seed, real_engine, real_client, monkeypatch)


@pytest.mark.parametrize("i", range(RANDOM_COUNT), ids=lambda i: f"random-{i}")
async def test_random_dispute_follow_ups_agree_with_the_reference_model(i, real_engine, real_client, monkeypatch):
    await _seed_case(_random_seed(i), real_engine, real_client, monkeypatch)
