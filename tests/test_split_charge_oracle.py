# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Generated sequences of Stripe follow-ups on a card charge a late success split across an
agreement's invoices, checked step by step against a reference model of what each part holds.

Each case is a seed. It picks a split (the amendment split: July holds 13.55 of the 30.00
charge, the amendment invoice 16.45; or the excess split: 10.00 paid by hand, July holds 20.00
and a 10.00 rest is kept among the unmatched payments), whether the charge is delivered at once
or after some follow-ups arrived, and then a sequence in any order of refunds, their reversals,
the steps of a dispute (won, lost or closed with a warning, with or without the funds given
back), a release (Stripe disconnected), replays of any earlier delivery and, for the excess
split, a person recording the rest on August. After every step the in-process API must agree
with the model: what every invoice owes, the unmatched payments, the kept refunds, the disputes
each payment carries, what each document posted to the deposit account, Disputed payments and
Payment dispute losses, and a balanced trial balance; no part ever holds less than nothing.

The model is plain arithmetic over the rules the follow-ups are documented to keep: a follow-up
is applied over the parts in reverse spill order (the rest first, the charge's own invoice
last), each part never past what it still holds (its payment, net of the refunds it gave back);
a refund past the whole charge, a reversal of a refund never applied, a change Stripe made
after the release on an invoice, or one before its charge is delivered is kept whole and tried
again when its charge arrives and after every refund applied; a dispute stays on the parts its
first recorded step placed it on, and the rest's share is told, not written, while it is
unmatched. It never calls the code it checks.

The fixed seeds always run; SPLIT_ORACLE_RANDOM more (default 4) are drawn from
SPLIT_ORACLE_SEED, else the commit (GITHUB_SHA) or the xdist run id, so every runner of one run
draws the same cases, else at random. A failing case names its seed: SPLIT_ORACLE_SEED=<seed>
with SPLIT_ORACLE_RANDOM=1 replays it.
"""
from __future__ import annotations

import hashlib
import os
import random
import secrets
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "default_modules" / "celerp-subscriptions" / "tests"))
from agreement_kit import tick  # noqa: E402
from test_collection import _attempt_outcome, _pay, cloud  # noqa: E402,F401  (cloud: fixture)
from test_collection_settlement import _amend, _docs, _july, _unmatched  # noqa: E402

pytestmark = pytest.mark.asyncio

FIXED_SEEDS = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89]
AT = "2026-07-20T10:00:00+00:00"
RELEASED_AT = "2026-07-10T00:00:00+00:00"  # before every refund here: none applies on an invoice after it


RANDOM_COUNT = int(os.environ.get("SPLIT_ORACLE_RANDOM", "4"))


def _random_seed(i: int) -> int:
    """The i-th random case's seed, drawn when the case runs so its test id stays random-<i>."""
    base = (os.environ.get("SPLIT_ORACLE_SEED") or os.environ.get("GITHUB_SHA")
            or os.environ.get("PYTEST_XDIST_TESTRUNUID"))
    if base and base.isdigit() and RANDOM_COUNT == 1:
        return int(base)
    if base is None:
        return secrets.randbelow(10**9)
    return int(hashlib.sha256(f"{base}:{i}".encode()).hexdigest()[:8], 16)


# ── Reference model ───────────────────────────────────────────────────────────


@dataclass
class Part:
    name: str  # "july", "amendment" or "rest"
    paid: int  # what the charge put on it
    doc: str | None  # its invoice; None for the rest while it is unmatched
    refunded: int = 0  # given back on its invoice
    released: bool = False
    dispute: int | None = None  # the share of the dispute it carries
    steps: set = field(default_factory=set)
    withdrawn: int | None = None


@dataclass
class Model:
    parts: list[Part]  # spill order
    owed: dict  # invoice -> cents owed
    ledger: dict  # invoice -> {"bank"|"disputed"|"losses": cents}
    delivered: bool
    kept: dict = field(default_factory=dict)  # (refund id, transition) -> (reference, cents) kept whole
    rest_kept: dict = field(default_factory=dict)  # (refund id, transition) -> cents kept with the rest
    shares: dict = field(default_factory=dict)  # refund id -> {part name: cents} of its applied change
    done: set = field(default_factory=set)  # (refund id, transition) applied
    rest_unmatched: bool = False
    rest_applied: set = field(default_factory=set)  # refund ids the rest gave back on its invoice

    def part(self, name: str) -> Part:
        return next(p for p in self.parts if p.name == name)

    def holds(self, p: Part) -> int:
        if p.doc is None:
            net = sum(c if t == "applied" else -c for (_r, t), c in self.rest_kept.items())
            held = p.paid - net
        else:
            held = p.paid - p.refunded
        assert held >= 0, f"{p.name} holds {held}"
        return held

    def _move(self, p: Part, cents: int) -> None:
        """Give back *cents* on the invoice of *p* (a reversal: negative)."""
        p.refunded += cents
        self.owed[p.doc] += cents
        self.ledger[p.doc]["bank"] -= cents

    def refund(self, rid: str, transition: str, cents: int, *, drain: bool = True) -> bool:
        """Deliver a change to a refund of the charge: True when it applied (or had)."""
        if (rid, transition) in self.done:
            return True
        ok = self.delivered and self._apply(rid, transition, cents)
        if not ok:
            self.kept.setdefault((rid, transition), ("charge", cents))
            return False
        self.kept.pop((rid, transition), None)
        if drain:
            self.drain()
        return True

    def drain(self) -> None:
        for (rid, transition), (_ref, cents) in sorted(self.kept.items()):
            if self._apply(rid, transition, cents):
                del self.kept[(rid, transition)]

    def _apply(self, rid: str, transition: str, cents: int) -> bool:
        if transition == "applied":
            shares, left = {}, cents
            for p in reversed(self.parts):
                if share := min(left, self.holds(p)):
                    shares[p.name], left = share, left - share
            if left:
                return False
        else:
            applied = self.shares.get(rid, {})
            shares = {n: c for n, c in applied.items() if self._took(rid, self.part(n))}
            if not shares or sum(shares.values()) != cents:
                return False
        if any(self.part(n).doc is not None and self.part(n).released for n in shares):
            return False
        sign = 1 if transition == "applied" else -1
        for name, share in shares.items():
            p = self.part(name)
            if p.doc is None:
                self.rest_kept[(rid, transition)] = share
            else:
                self._move(p, sign * share)
                if name == "rest" and transition == "applied":
                    self.rest_applied.add(rid)
        if transition == "applied":
            self.shares[rid] = shares
        self.done.add((rid, transition))
        return True

    def _took(self, rid: str, p: Part) -> bool:
        """Whether *p* still shows the applied refund *rid* took from it: on its invoice, or
        kept with the rest."""
        if p.name != "rest":
            return True
        return (rid, "applied") in self.rest_kept if p.doc is None else rid in self.rest_applied

    def record_rest(self, august: str) -> None:
        """A person records the rest on August: it is paid there, then its kept refunds
        apply in order, each unless the rest no longer links to Stripe."""
        rest = self.part("rest")
        rest.doc, self.rest_unmatched = august, False
        self.owed[august] -= rest.paid
        self.ledger[august]["bank"] += rest.paid
        for (rid, transition), cents in sorted(self.rest_kept.items()):
            if rest.released:
                continue
            self._move(rest, cents if transition == "applied" else -cents)
            del self.rest_kept[(rid, transition)]
            if transition == "applied":
                self.rest_applied.add(rid)

    def release(self) -> None:
        for p in self.parts:
            p.released = True
        self.drain()

    def dispute(self, state: str, outcome: str | None, cents: int) -> None:
        carried = [p for p in self.parts if p.doc is not None and p.dispute is not None]
        if carried:
            shares = [(p, p.dispute) for p in carried]
        else:
            shares, left = [], cents
            for p in reversed(self.parts):
                if share := min(left, self.holds(p)):
                    shares.append((p, share))
                    left -= share
            if left:
                return  # more than the charge holds: told, nothing written
        for p, share in shares:
            if p.doc is None:
                continue  # the rest's share while it is unmatched: told, not written
            step = state if state != "closed" else f"closed:{outcome}"
            if step in p.steps:
                continue
            taken = "funds_withdrawn" in p.steps
            back = "funds_reinstated" in p.steps
            book = self.ledger[p.doc]
            if state == "funds_withdrawn" and not taken:
                p.withdrawn = share
                book["bank"] -= share
                book["disputed"] += share
            elif state == "funds_reinstated" and taken and not back:
                book["bank"] += p.withdrawn
                book["disputed"] -= p.withdrawn
            elif state == "closed" and outcome == "lost" and taken and not back:
                book["losses"] += p.withdrawn
                book["disputed"] -= p.withdrawn
            p.dispute = share
            p.steps.add(step)


# ── Sequences ─────────────────────────────────────────────────────────────────


def _sequence(rng: random.Random, excess: bool, late: bool) -> list[tuple]:
    """The follow-ups of one case: ("deliver",), ("refund", id, cents), ("reverse", id),
    ("dispute", state, outcome), ("release",), ("record",), ("replay", index)."""
    held = [1355, 1645, 3000] if not excess else [1000, 2000, 3000]
    events, refunds, n = [], [], rng.randint(6, 11)
    steps = ["created", "funds_withdrawn"] + (["funds_reinstated"] if rng.random() < 0.3 else [])
    steps.append(("closed", rng.choice(["won", "lost", "lost", "warning_closed"])))
    deliver_at = rng.randint(1, 3) if late else 0
    recorded = False
    for i in range(n):
        if i == deliver_at:
            events.append(("deliver",))
        delivered = i >= deliver_at
        kinds = ["refund"] * 6 + ["reverse"] * 3 + ["replay"] * 3
        if delivered:
            kinds += ["dispute"] * 5 + ["release"]
            if excess and not recorded:
                kinds += ["record"] * 2
        kind = rng.choice(kinds)
        if kind == "refund":
            rid = f"re_{len(refunds):02d}"
            cents = rng.choice([rng.randint(1, 3001), rng.choice(held), rng.randint(1, 600)])
            refunds.append((rid, cents))
            events.append(("refund", rid, cents))
        elif kind == "reverse" and refunds:
            events.append(("reverse", *rng.choice(refunds)))
        elif kind == "reverse":
            events.append(("reverse", "re_zz", 500))  # never applied
        elif kind == "dispute" and steps:
            for step in steps[:rng.randint(1, 2)]:
                events.append(("dispute", *(step if isinstance(step, tuple) else (step, None))))
                steps.remove(step)
        elif kind == "release":
            events.append(("release",))
        elif kind == "record":
            recorded = True
            events.append(("record",))
        elif kind == "replay" and events:
            j = rng.randrange(len(events))
            events.append(("replay", j) if events[j][0] != "replay" else events[j])
    if deliver_at >= n:
        events.append(("deliver",))
    return events


# ── The case ──────────────────────────────────────────────────────────────────


async def _books(client, hd, docs, accounts) -> dict:
    out = {}
    for d in docs:
        r = await client.get(f"/accounting/journal/by-doc/{d}", headers=hd)
        assert r.status_code == 200, r.text
        net = {k: 0 for k in accounts}
        for entry in r.json()["entries"]:
            for line in entry["lines"]:
                for name, code in accounts.items():
                    if line["account"] == code:
                        net[name] += round(float(line["debit"] or 0) * 100) - round(float(line["credit"] or 0) * 100)
        out[d] = net
    return out


def _carried(doc_state: dict, reference: str) -> list[int]:
    payment = next((p for p in doc_state.get("payments", []) if p.get("reference") == reference
                    and p.get("status") != "deleted"), None)
    if payment is None:
        return []
    assert round(float(payment.get("amount") or 0) * 100) >= round(float(payment.get("refunded") or 0) * 100)
    return sorted(int(x["amount_minor"]) for x in (payment.get("disputes") or {}).values())


async def _seed_case(seed: int, client, session, monkeypatch, cloud):
    from celerp.services import payments
    from celerp.services.account_roles import AccountRole as R, resolve
    from celerp.services.payments import unmatched_refunds
    rng = random.Random(seed)
    excess, late = rng.random() < 0.5, rng.random() < 0.35
    hd, cid, eid, july = await _july(client, session, monkeypatch, cloud)
    [sent] = cloud.sent("attempt")
    books = sent["context"]
    cloud.answers["cancel"] = (409, {"detail": "attempt_submitted"})
    if excess:
        await _pay(client, hd, july, 10.0)
        await tick(session, cid, "2026-07-02")
        other, paid_at = None, "2026-07-02T13:00:00+00:00"
        split = [("july", 2000, july), ("rest", 1000, None)]
    else:
        other = (await _amend(client, monkeypatch, hd, eid)).json()["invoice_id"]
        await tick(session, cid, "2026-07-15")
        assert await _attempt_outcome(cid, july, 1, "succeeded") is True
        paid_at = "2026-07-15T13:00:00+00:00"
        split = [("july", 1355, july), ("amendment", 1645, other)]
    ref = f"pi_{seed}"
    charge = {"company_id": cid, "entity_id": july, "reference": ref, "amount_minor": 3000, "currency": "USD",
              "paid_at": paid_at, "managed": True, "context": books}
    accounts = {"bank": books["deposit_account"], "disputed": await resolve(session, uuid.UUID(cid),
                                                                              R.DISPUTED_PAYMENTS),
                "losses": await resolve(session, uuid.UUID(cid), R.PAYMENT_DISPUTE_LOSSES)}
    await session.rollback()
    docs = [july] + ([other] if other else [])
    state = await _docs(session, eid)
    model = Model(parts=[Part(n, c, d if d else None) for n, c, d in split],
                  owed={d: round(state[d]["amount_outstanding"] * 100) for d in docs},
                  ledger={d: {k: 0 for k in accounts} for d in docs}, delivered=False)
    base = await _books(client, hd, docs, accounts)
    dispute_cents = rng.choice([rng.randint(1, 3000), 3000, rng.choice([1000, 1355, 1645, 2000])])
    august = None
    events = _sequence(rng, excess, late)
    trail = []

    async def deliver(event):
        nonlocal august
        kind = event[0]
        if kind == "deliver":
            assert await payments.receive_payment(charge)
            if not model.delivered:
                model.delivered = True
                for p in model.parts:
                    if p.doc is not None:
                        model.owed[p.doc] -= p.paid
                        model.ledger[p.doc]["bank"] += p.paid
                model.rest_unmatched = excess
                model.drain()
        elif kind in ("refund", "reverse"):
            transition = "applied" if kind == "refund" else "reversed"
            payload = {"context": books, "company_id": cid, "entity_id": july, "reference": ref,
                       "refund_id": event[1], "transition": transition, "amount_minor": event[2], "cycle": 1,
                       "currency": "USD", "occurred_at": AT}
            assert await payments.receive_refund(payload)
            model.refund(event[1], transition, event[2])
        elif kind == "dispute":
            got = await payments.receive_dispute({
                "company_id": cid, "entity_id": july, "reference": ref, "dispute_id": "dp_1", "state": event[1],
                "outcome": event[2], "amount_minor": dispute_cents, "currency": "usd", "occurred_at": AT})
            assert got is model.delivered
            if model.delivered:
                model.dispute(event[1], event[2], dispute_cents)
        elif kind == "release":
            got = await payments.receive_release({"company_id": cid, "entity_id": july, "reference": ref,
                                                  "released_at": RELEASED_AT})
            assert got is model.delivered
            if model.delivered and not model.parts[0].released:
                model.release()
        elif kind == "record":
            await tick(session, cid, "2026-08-01")
            august = next(d for d, s in (await _docs(session, eid)).items()
                          if d not in docs and s.get("doc_type") == "invoice")
            before = (await _docs(session, eid))[august]
            docs.append(august)
            model.owed[august] = round(before["amount_outstanding"] * 100)
            model.ledger[august] = {k: 0 for k in accounts}
            base.update(await _books(client, hd, [august], accounts))
            await payments.record_unmatched_payment(session, uuid.UUID(cid), f"{ref}:rest", august)
            await session.commit()
            model.record_rest(august)
        elif kind == "replay" and events[event[1]][0] != "record":  # a person records the rest once
            await deliver(events[event[1]])

    for event in events:
        trail.append(event)
        await deliver(event)
        state = await _docs(session, eid)
        seen = {
            "owed": {d: round(state[d]["amount_outstanding"] * 100) for d in docs},
            "unmatched": await _unmatched(session),
            "kept": sorted((r.reference, r.amount_minor) for r in await unmatched_refunds(session)),
            "disputes": {d: _carried(state[d], f"{ref}:rest" if d == august else ref) for d in docs},
            "books": {d: {k: v - base[d][k] for k, v in net.items()}
                      for d, net in (await _books(client, hd, docs, accounts)).items()},
        }
        expected = {
            "owed": model.owed,
            "unmatched": [(f"{ref}:rest", 1000)] if model.rest_unmatched else [],
            "kept": sorted([(ref, c) for _r, c in model.kept.values()]
                           + [(f"{ref}:rest", c) for c in model.rest_kept.values()]),
            "disputes": {p.doc: [p.dispute] if p.dispute is not None else [] for p in model.parts if p.doc},
            "books": model.ledger,
        }
        expected["disputes"] = {d: expected["disputes"].get(d, []) for d in docs}
        assert seen == expected, f"seed {seed} after {trail}"
        tb = (await client.get("/accounting/trial-balance", headers=hd)).json()
        assert tb.get("balanced") is True, f"seed {seed} after {trail}: {tb}"


@pytest.mark.parametrize("seed", FIXED_SEEDS)
async def test_split_charge_follow_ups_agree_with_the_reference_model(seed, client, session, monkeypatch, cloud):
    await _seed_case(seed, client, session, monkeypatch, cloud)


@pytest.mark.parametrize("i", range(RANDOM_COUNT), ids=lambda i: f"random-{i}")
async def test_random_split_charge_follow_ups_agree_with_the_reference_model(i, client, session, monkeypatch, cloud):
    await _seed_case(_random_seed(i), client, session, monkeypatch, cloud)
