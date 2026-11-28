"""A seeded generative oracle for what a foreign journal line means.

A line carrying a foreign amount F at a rate R beside a books amount B means one figure: it is
accepted only when B is F x R up to what rounding each stored figure allows. Random lines go
through both doors a person uses, the journal import and the typed entry, and the test judges
each against its own reference predicate, computed here in Decimal from the stored precision of
each figure. It asserts:

- the import accepts a line exactly when the reference predicate holds;
- the typed door, which works out B itself, stores the line the import stores for the same figures;
- for each foreign currency, the books total (GL) of its lines and what the bank list reads from
  them (``_bank_amounts`` on a bank kept in that currency: each foreign amount at its stored rate)
  differ by no more than the lines' bounds.
"""

from __future__ import annotations

import random
import uuid
from decimal import ROUND_HALF_UP, Decimal

import pytest

from celerp.models.projections import Projection
from celerp.services.currencies import CURRENCIES

pytestmark = pytest.mark.asyncio

SEED = 20261128
CASES_PER_BASE = 130
# Precision as ISO 4217 states it, written out here so the predicate does not lean on the product.
_DP = {**dict.fromkeys(("BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND", "VUV",
                         "XAF", "XOF", "XPF"), 0),
       **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3), "CLF": 4}
_RATE_DP = 12


def _dp(currency: str) -> int:
    return _DP.get(currency, 2)


def _q(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP)


def _unit(currency: str) -> Decimal:
    return Decimal(10) ** -_dp(currency)


def _sig(value: Decimal, places: int) -> int:
    """Places that keep *value* within 15 significant digits, so a float carries it exactly."""
    whole = len(str(int(abs(value)))) if abs(value) >= 1 else 0
    return max(0, min(places, 15 - whole))


def _bound(fx: Decimal, rate: Decimal, base: str, currency: str) -> Decimal:
    return _unit(base) + rate * _unit(currency) + fx * Decimal(10) ** -_RATE_DP / 2


def _means_one_figure(books: Decimal, fx: Decimal, rate: Decimal, base: str, currency: str) -> bool:
    """The reference predicate on the figures as they are stored."""
    stored_fx, stored_books, stored_rate = _q(fx, _dp(currency)), _q(books, _dp(base)), _q(rate, _RATE_DP)
    return abs(stored_books - stored_fx * stored_rate) <= _bound(stored_fx, stored_rate, base, currency)


def _log_uniform(rng: random.Random, low: float, high: float) -> Decimal:
    import math

    return Decimal(repr(math.exp(rng.uniform(math.log(low), math.log(high)))))


def _case(rng: random.Random, base: str, currencies: list[str]) -> dict:
    """One random line: a currency, F, R given to 2..12 places, and B exact, rounded or perturbed."""
    while True:
        currency = rng.choice(currencies)
        raw_fx = _log_uniform(rng, 1e-2, 1e12)
        fx = _q(raw_fx, _sig(raw_fx, _dp(currency) + rng.choice((0, 0, 1, 2))))
        rate = _log_uniform(rng, 1e-6, 1e4)
        rate = _q(rate, _sig(rate, rng.randint(2, _RATE_DP)))
        stored_fx = _q(fx, _dp(currency))
        if not stored_fx or not rate or not Decimal("0.01") <= stored_fx * rate <= Decimal("1e11"):
            continue
        exact = stored_fx * rate
        bound = _bound(stored_fx, rate, base, currency)
        mode = rng.choice(("exact", "raw", "near", "near", "far"))
        if mode == "exact":
            books = exact
        elif mode == "raw":
            books = fx * rate
        elif mode == "near":  # around the bound, a few books' units either side of it
            books = exact + rng.choice((-1, 1)) * (bound + rng.randint(-2, 2) * _unit(base))
        else:
            books = exact * Decimal(repr(rng.choice((0.5, 0.9, 0.999, 1.001, 1.1, 2.0))))
        books = _q(books, _dp(base))
        typed_books = _q(stored_fx * rate, _dp(base))
        if books > 0 and typed_books > 0:
            return {"currency": currency, "fx": fx, "rate": rate, "books": books, "typed_books": typed_books,
                    "side": rng.choice(("debit", "credit"))}


def _lines(case: dict, books: Decimal, typed: bool) -> list[dict]:
    side, other = case["side"], "credit" if case["side"] == "debit" else "debit"
    if typed:
        foreign = {"account": "6100", side: float(case["fx"]), "currency": case["currency"], "rate": float(case["rate"])}
    else:
        foreign = {"account": "6100", side: float(books), f"fx_{side}": float(case["fx"]),
                   "fx_currency": case["currency"], "fx_rate": float(case["rate"])}
    return [foreign, {"account": "4100", other: float(books)}]


async def _stored_line(session, company_id, entity_id: str) -> dict | None:
    row = await session.get(Projection, (company_id, entity_id))
    return row.state["entries"][0] if row is not None else None


def _money(line: dict) -> dict:
    """A stored line's figures; an empty side is 0 typed and absent imported."""
    return {k: line.get(k) or None for k in ("debit", "credit", "fx_debit", "fx_credit", "fx_currency", "fx_rate")}


@pytest.mark.parametrize("base", ["USD", "JPY", "KWD", "THB"])
async def test_a_foreign_line_is_accepted_exactly_when_its_books_amount_is_its_amount_at_its_rate(
        client, session, auth, base):
    from celerp.services.company_lock import locked_company
    from celerp_accounting.routes import _bank_amounts

    company_id = uuid.UUID(str(auth["company_id"]))
    company = await locked_company(session, company_id)
    company.settings = {**(company.settings or {}), "currency": base}
    await session.commit()

    rng = random.Random(f"{SEED}:{base}")
    currencies = sorted({*CURRENCIES, "JPY", "VND", "KWD"} - {base})
    cases = [_case(rng, base, currencies) for _ in range(CASES_PER_BASE)]
    for case in cases:
        case["je"] = f"je:{uuid.uuid4()}"
    for start in range(0, len(cases), 50):
        r = await client.post("/accounting/import/batch", headers=auth["headers"], json={"records": [{
            "entity_id": case["je"], "event_type": "acc.journal_entry.created", "source": "csv",
            "idempotency_key": uuid.uuid4().hex,
            "data": {"status": "posted", "ts": "2026-03-01", "memo": "oracle",
                     "entries": _lines(case, case["books"], typed=False)}} for case in cases[start:start + 50]]})
        assert r.status_code == 200, r.text

    held: dict[str, list[dict]] = {}
    for case in cases:
        imported = await _stored_line(session, company_id, case["je"])
        expected = _means_one_figure(case["books"], case["fx"], case["rate"], base, case["currency"])
        assert (imported is not None) == expected, (base, case, imported)
        typed = await client.post("/accounting/journal-entries", headers=auth["headers"], json={
            "ts": "2026-03-01", "memo": "oracle", "idempotency_token": uuid.uuid4().hex,
            "entries": _lines(case, case["typed_books"], typed=True)})
        assert typed.status_code == 200, (base, case, typed.text)
        typed_line = await _stored_line(session, company_id, typed.json()["je_id"])
        held.setdefault(case["currency"], []).append(typed_line)
        if imported is not None:
            held[case["currency"]].append(imported)
            if case["books"] == case["typed_books"]:
                assert _money(typed_line) == _money(imported), (base, case, typed_line, imported)

    for currency, lines in held.items():
        gl = sum(Decimal(str(ln.get("debit") or 0)) - Decimal(str(ln.get("credit") or 0)) for ln in lines)
        bank = slack = Decimal(0)
        for ln in lines:
            fx_in, fx_out = _bank_amounts(ln, currency, base)
            rate = Decimal(str(ln["fx_rate"]))
            bank += (fx_in - fx_out) * rate
            slack += _bound(fx_in + fx_out, rate, base, currency)
        assert abs(gl - bank) <= slack, (base, currency, gl, bank, slack)
