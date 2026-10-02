# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company connected to Celerp Cloud has its online invoice payments closed at Cloud
before it is reset. Once the reset has committed, Cloud closes them for good; when it
does not commit, Cloud reopens them, so a company that stays keeps taking payments.
When Celerp Cloud cannot confirm, nothing is deleted. A reset interrupted part way is
settled the same way on the next start or the next reconciliation. A System Recovery
restore is reported to Cloud, which reopens the payments of the companies it brought
back; until Cloud confirms it, no company can be reset. A payment Celerp Cloud delivers
for a company or invoice that no longer exists is kept among the unmatched payments, and
holds the company's closing until it is recorded. A restore has Celerp Cloud deliver again
the payments recorded since its backup started; no new payment opens until they are
recorded, and none opens while Cloud has not confirmed the restore. Each payment is
recorded only from Cloud's delivery, on the books its payment page opened with, so one
recorded again after a restore posts exactly as it first did; a delivery without usable
books is kept among the unmatched payments."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from company_backup_support import company, owner, snapshot, token
from migration_support import auth, code_config, maker, real_client, real_engine  # noqa: F401

from celerp.services.payments import reconcile_payments

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
CLOSURE = "/billing/connect/companies/retire/"
RECOVERY = "/billing/connect/recovery"
CHECKOUT = "/billing/connect/checkout"
PAUSED = "Online payment is paused while recent payments are checked. Please try again shortly."
NAME = "Harbor Goods Ltd"
LOST = object()  # Cloud takes the step, but its answer never arrives
NO_ANSWER = object()  # the request returns nothing


_PAYMENT = ("company_id", "entity_id", "reference", "amount_minor", "currency", "paid_at", "context",
            "delivery_id")
# The books a payment page opened with, for a payment whose page the test does not open.
BOOKS = {"deposit_account": "1110", "timezone": "UTC", "base_currency": "USD", "rate": "1"}
OPENED = object()  # the books the invoice's last payment page opened with


class _Crash(BaseException):
    """The process stops."""


class _Cloud:
    """Celerp Cloud as the installation reaches it: each closing request moves from
    prepared to retired or cancelled, as Cloud does, under the installation's payment
    generation, which each reported System Recovery restore advances. A restore has
    every payment the installation ever recorded delivered again, and is confirmed
    only once the installation has recorded every delivered payment; until then no
    payment opens, and one opens only for the current generation. A scripted answer
    replaces the next answer to one step."""

    def __init__(self, monkeypatch, engine, *, credential: str = "cloud-credential") -> None:
        from celerp.services import cloud_entitlement
        self.engine = engine
        self.ops: dict[str, dict] = {}  # operation -> company, state, generation
        self.deliveries: list[dict] = []  # payments delivered to the installation, with "acked"
        self.generation = 0
        self.recoveries: dict[str, tuple[int, list[str]]] = {}
        self.confirmed: set[str] = set()  # recoveries confirmed
        self.checkouts: list[tuple[str, int, int]] = []  # (company, generation, status) per payment asked to open
        self.opened: dict[str, dict] = {}  # invoice -> the books its last payment page opened with
        self.scripted: dict[str, list] = {"prepare": [], "finalize": [], "cancel": [], "recovery": []}
        self.calls: list[tuple[str, str]] = []  # (step, operation or recovery)
        self.reads: list[str] = []  # anything the installation asked to read
        self.generations: list[int] = []  # the generation each closing step carried
        self.company_present: list[bool] = []
        self.on_prepared = None

        async def stored_api_key() -> str:
            return credential

        monkeypatch.setattr(cloud_entitlement, "stored_api_key", stored_api_key)
        monkeypatch.setattr(cloud_entitlement, "authenticated_request", self._request)

    def payments_open(self, company_id) -> bool:
        return not any(o["company"] == str(company_id) and o["state"] != "cancelled" for o in self.ops.values())

    def states(self) -> list[str]:
        return sorted(o["state"] for o in self.ops.values())

    def pay(self, company_id, entity_id: str = "doc:gone", reference: str = "pi_late",
            amount_minor: int = 107000, paid_at: datetime | None = None, books=OPENED) -> None:
        """A customer pays: Celerp Cloud delivers the payment, with when Stripe reported
        it paid and the books its page opened with, until the installation acknowledges
        it."""
        self.deliveries.append({"company_id": str(company_id), "entity_id": entity_id, "reference": reference,
                                "amount_minor": amount_minor, "currency": "usd",
                                "paid_at": (paid_at or datetime.now(timezone.utc)).isoformat(),
                                "context": self.opened.get(entity_id, BOOKS) if books is OPENED else books,
                                "delivery_id": str(uuid.uuid4()), "acked": False})

    async def deliver(self) -> None:
        """Deliver every payment not yet acknowledged, as the gateway receives it."""
        from celerp.gateway.client import GatewayClient
        gateway = GatewayClient(gateway_token="t", instance_id="i", gateway_url="wss://relay.invalid/ws")
        gateway._ws = object()
        acked = set()

        async def send(ws, message):
            assert message["type"] == "event.ack"
            acked.add(message["payload"]["delivery_id"])
        gateway._send = send
        for d in [d for d in self.deliveries if not d["acked"]]:
            await gateway._handle_invoice_payment({k: v for k, v in d.items() if k in _PAYMENT})
            d["acked"] = d["delivery_id"] in acked
            d["delivered_at"] = datetime.now(timezone.utc) if d["acked"] else None

    def _unrecorded(self, company_id: str) -> bool:
        return any(d["company_id"] == company_id and not d["acked"] for d in self.deliveries)

    @staticmethod
    def _refuse(detail: str) -> httpx.Response:
        return httpx.Response(409, json={"detail": detail})

    def _take(self, step: str, company_id: str, op: str, generation: int) -> httpx.Response:
        row = self.ops.get(op)
        if step == "prepare":
            if row is None:
                if generation != self.generation:
                    return self._refuse("generation_stale")
                if any(o["company"] == company_id and o["state"] in ("prepared", "retired")
                       for o in self.ops.values()):
                    return self._refuse("closure_pending")
                row = self.ops[op] = {"company": company_id, "state": "prepared", "generation": generation}
            elif row["state"] == "cancelled":
                return self._refuse("cancelled")
        elif step == "finalize":
            if row is None:
                return self._refuse("not_prepared")
            if row["state"] == "cancelled":
                return self._refuse("cancelled")
            if row["state"] == "prepared":
                if generation != row["generation"] or generation != self.generation:
                    return self._refuse("generation_stale")
                if self._unrecorded(company_id):
                    return self._refuse("payment_unrecorded")
                row["state"] = "retired"
        else:
            if row is None:
                row = self.ops[op] = {"company": company_id, "state": "cancelled", "generation": generation}
            elif row["state"] == "retired":
                return self._refuse("retired")
            row["state"] = "cancelled"
        return httpx.Response(200, json={"company_id": company_id, "operation_id": op, "state": row["state"]})

    def _recover(self, recovery_id: str, company_ids: list[str]) -> httpx.Response:
        if recovery_id not in self.recoveries:
            self.generation += 1
            self.recoveries[recovery_id] = (self.generation, company_ids)
            for o in self.ops.values():
                if o["company"] in company_ids and o["state"] in ("prepared", "retired"):
                    o["state"] = "cancelled"
            self.deliveries += [{**d, "delivery_id": str(uuid.uuid4()), "acked": False, "replay": True}
                                for d in self.deliveries if d["acked"]]
        if any(not d["acked"] for d in self.deliveries):
            return self._refuse("payment_unrecorded")
        self.confirmed.add(recovery_id)
        return httpx.Response(200, json={"recovery_id": recovery_id, "generation": self.recoveries[recovery_id][0]})

    def _checkout(self, body: dict) -> httpx.Response:
        company_id, generation = body["company_id"], body["generation"]
        if not set(self.recoveries) <= self.confirmed:
            response = self._refuse("recovery_pending")
        elif generation != self.generation:
            response = self._refuse("generation_stale")
        else:
            self.opened[body["entity_id"]] = body["context"]
            response = httpx.Response(200, json={"url": "https://checkout.stripe.test/cs_1"})
        self.checkouts.append((company_id, generation, response.status_code))
        return response

    async def _request(self, method, path, *, total_s=None, json=None, params=None, api_key=None):
        if method != "POST":
            self.reads.append(path)
            return httpx.Response(404, json={"detail": "Not Found"})
        if path == CHECKOUT:
            return self._checkout(json)
        if path == RECOVERY:
            step, key = "recovery", json["recovery_id"]
        else:
            assert path.startswith(CLOSURE)
            step, key = path.removeprefix(CLOSURE), json["operation_id"]
            self.generations.append(json["generation"])
            async with maker(self.engine)() as s:
                self.company_present.append(bool(await s.scalar(
                    text("SELECT count(*) FROM companies WHERE id = :c"), {"c": json["company_id"]})))
        self.calls.append((step, key))
        answer = self.scripted[step].pop(0) if self.scripted[step] else None
        if answer is NO_ANSWER:
            return None
        if answer is not None and answer is not LOST:
            if isinstance(answer, Exception):
                raise answer
            return answer
        if step == "recovery":
            assert set(json) == {"recovery_id", "company_ids"}
            response = self._recover(key, json["company_ids"])
        else:
            response = self._take(step, json["company_id"], key, json["generation"])
        if answer is LOST:
            raise httpx.ReadTimeout("no answer")
        if step == "prepare" and response.status_code == 200 and self.on_prepared:
            await self.on_prepared()
        return response


async def _harbor(engine):
    from celerp_accounting.routes import seed_chart_of_accounts_hook
    boss = await owner(engine)
    a = await company(engine, boss, NAME, "alpha")
    b = await company(engine, boss, "Hillside Supply Co", "bravo")
    async with maker(engine)() as s:  # each keeps books, as a company made in Celerp does
        for cid in (a, b):
            await seed_chart_of_accounts_hook(session=s, company_id=cid)
        await s.commit()
    return boss, a, b


async def _companies(engine) -> set[str]:
    async with maker(engine)() as s:
        return {str(c) for c in (await s.scalars(text("SELECT id FROM companies"))).all()}


async def _closures(engine) -> list[str]:
    async with maker(engine)() as s:
        return [str(c) for c in (await s.scalars(text("SELECT target_company FROM payment_closures"))).all()]


async def _reset(client, engine, boss, cid):
    return await client.post(RESET, json={"company_name": NAME}, headers=auth(await token(engine, boss, cid)))


def _steps(cloud) -> list[str]:
    return [step for step, _ in cloud.calls]


def _one_operation(cloud) -> None:
    assert len({op for _, op in cloud.calls}) == 1


async def test_a_reset_prepares_the_closing_then_closes_the_payments_once_the_company_is_gone(real_engine, real_client,
                                                                                    monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _steps(cloud) == ["prepare", "finalize"]
    _one_operation(cloud)
    assert cloud.company_present == [True, False]  # prepared before anything was deleted
    assert cloud.states() == ["retired"]
    assert await _companies(real_engine) == {str(b)}
    assert await _closures(real_engine) == []


@pytest.mark.parametrize("answer, status, says", [
    (NO_ANSWER, 503, "could not confirm"),
    (httpx.ConnectError("unreachable"), 503, "could not confirm"),
    (httpx.Response(502, json={"detail": "Could not confirm"}), 503, "could not confirm"),
    (httpx.Response(500, text="Internal Server Error"), 503, "could not confirm"),
    (httpx.Response(409, json={"detail": "payment_settling"}), 409, "still being processed."),
    (httpx.Response(409, json={"detail": "payment_unrecorded"}), 409, "has not reached Celerp"),
    (httpx.Response(409, json={"detail": "reconnect_required"}), 409, "Reconnect this Stripe account"),
    (httpx.Response(409, json={"detail": "something else"}), 503, "could not confirm"),
    (httpx.Response(200, json={"company_id": "another-company", "operation_id": "x", "state": "prepared"}),
     503, "could not confirm"),
    (httpx.Response(200, json={"state": "cancelled"}), 503, "could not confirm"),
    (httpx.Response(200, text="<html>proxy</html>"), 503, "could not confirm"),
], ids=["no-answer", "unreachable", "stripe-unconfirmed", "server-error", "settling", "unrecorded",
        "reconnect", "unknown-refusal", "other-company", "not-prepared", "not-json"])
async def test_nothing_is_deleted_unless_cloud_confirms_the_closing_is_prepared(
        real_engine, real_client, monkeypatch, answer, status, says):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [answer]
    tok = await token(real_engine, boss, a)
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok))

    assert r.status_code == status, r.text
    assert says in r.json()["detail"] and r.json()["detail"].endswith("Nothing was deleted.")
    assert _steps(cloud) == ["prepare", "cancel"]
    _one_operation(cloud)
    assert cloud.payments_open(a)
    assert await snapshot(real_engine) == before


async def test_a_prepare_whose_answer_is_lost_is_reopened_and_nothing_is_deleted(real_engine, real_client,
                                                                               monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 503 and "could not confirm" in r.json()["detail"]
    assert _steps(cloud) == ["prepare", "cancel"]
    assert cloud.states() == ["cancelled"]
    assert cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}
    assert await _closures(real_engine) == []


async def test_a_lost_prepare_that_cannot_be_reopened_stays_closed_until_the_next_start(real_engine, real_client,
                                                                                      monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]
    cloud.scripted["cancel"] = [httpx.ConnectError("unreachable")]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 503
    assert cloud.states() == ["prepared"] and not cloud.payments_open(a)
    assert await _closures(real_engine) == [str(a)]

    await reconcile_payments()  # the next start
    assert cloud.payments_open(a)
    assert await _closures(real_engine) == []
    assert await _companies(real_engine) == {str(a), str(b)}


async def test_a_sign_out_during_the_reset_keeps_the_company_and_reopens_its_payments(real_engine, real_client,
                                                                                     monkeypatch):
    from celerp.services.session_tracker import invalidate_sessions
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    async def sign_out():
        async with maker(real_engine)() as s:
            await invalidate_sessions(s, str(boss))
    cloud.on_prepared = sign_out

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 401, r.text
    assert _steps(cloud) == ["prepare", "cancel"]
    _one_operation(cloud)
    assert cloud.company_present == [True, True]
    assert cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}
    assert await _closures(real_engine) == []


def _commit_fails_after_the_prepare(monkeypatch, cloud, *, landed: bool):
    """The reset's commit, the first one after Cloud prepared the closing, reports a
    failure: before reaching the database, or after it committed (the answer lost)."""
    real = AsyncSession.commit
    armed = []

    async def prepared():
        armed.append(True)
    cloud.on_prepared = prepared

    async def commit(self):
        if armed:
            armed.clear()
            if landed:
                await real(self)
            raise OperationalError("COMMIT", None, ConnectionError("connection lost"))
        return await real(self)
    monkeypatch.setattr(AsyncSession, "commit", commit)


async def _status(call) -> int | Exception:
    try:
        return (await call).status_code
    except Exception as exc:
        return exc


async def test_a_reset_whose_commit_fails_after_the_prepare_reopens_the_payments(real_engine, real_client,
                                                                               monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    _commit_fails_after_the_prepare(monkeypatch, cloud, landed=False)

    assert await _status(_reset(real_client, real_engine, boss, a)) != 200

    assert _steps(cloud) == ["prepare", "cancel"]
    assert cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}
    assert await _closures(real_engine) == []


async def test_a_commit_that_landed_but_reported_failure_closes_the_payments_for_good(real_engine, real_client,
                                                                                     monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    _commit_fails_after_the_prepare(monkeypatch, cloud, landed=True)

    assert await _status(_reset(real_client, real_engine, boss, a)) != 200

    assert _steps(cloud) == ["prepare", "finalize"]
    assert cloud.states() == ["retired"]
    assert await _companies(real_engine) == {str(b)}
    assert await _closures(real_engine) == []


async def test_a_crash_after_the_commit_is_finalized_on_the_next_start(real_engine, real_client, monkeypatch):
    from celerp.services import payments
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    settle = payments.settle_company_closure

    async def crashed(operation_id):
        return False
    monkeypatch.setattr(payments, "settle_company_closure", crashed)

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert cloud.states() == ["prepared"] and not cloud.payments_open(a)  # stays closed meanwhile
    assert await _closures(real_engine) == [str(a)]

    monkeypatch.setattr(payments, "settle_company_closure", settle)
    await payments.reconcile_payments()  # the next start
    assert _steps(cloud) == ["prepare", "finalize"]
    _one_operation(cloud)
    assert cloud.states() == ["retired"]
    assert await _closures(real_engine) == []

    await payments.reconcile_payments()  # settling again is a no-op
    assert _steps(cloud) == ["prepare", "finalize"]


async def test_a_finalize_that_fails_is_repeated_on_the_next_start(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["finalize"] = [LOST]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert await _closures(real_engine) == [str(a)]
    await reconcile_payments()
    assert _steps(cloud) == ["prepare", "finalize", "finalize"]
    _one_operation(cloud)
    assert cloud.states() == ["retired"]
    assert await _closures(real_engine) == []


async def test_a_crash_before_the_commit_is_reopened_on_the_next_start(real_engine, real_client, monkeypatch):
    from celerp.services import payments
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    async def crash():
        raise _Crash()
    cloud.on_prepared = crash
    settle = payments.settle_company_closure

    async def crashed(operation_id):
        return False
    monkeypatch.setattr(payments, "settle_company_closure", crashed)

    with pytest.raises(_Crash):
        await _reset(real_client, real_engine, boss, a)
    assert not cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}

    monkeypatch.setattr(payments, "settle_company_closure", settle)
    await payments.reconcile_payments()  # the next start
    assert _steps(cloud) == ["prepare", "cancel"]
    assert cloud.payments_open(a)
    assert await _closures(real_engine) == []


async def test_a_reset_retried_after_a_crash_reopens_the_stale_request_first(real_engine, real_client,
                                                                            monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]
    cloud.scripted["cancel"] = [httpx.ConnectError("unreachable")]
    assert (await _reset(real_client, real_engine, boss, a)).status_code == 503
    stale = cloud.calls[0][1]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert cloud.calls[2:] == [("cancel", stale), ("prepare", cloud.calls[3][1]), ("finalize", cloud.calls[3][1])]
    assert cloud.calls[3][1] != stale
    assert cloud.states() == ["cancelled", "retired"]
    assert await _companies(real_engine) == {str(b)}
    assert await _closures(real_engine) == []


async def test_a_stale_request_that_cannot_be_reopened_refuses_the_reset(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]
    cloud.scripted["cancel"] = [httpx.ConnectError("unreachable"), httpx.ConnectError("unreachable")]
    assert (await _reset(real_client, real_engine, boss, a)).status_code == 503

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 503 and r.json()["detail"].endswith("Nothing was deleted.")
    assert _steps(cloud) == ["prepare", "cancel", "cancel"]
    assert not cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}


async def test_a_disconnected_installation_is_told_to_reconnect_and_nothing_is_deleted(real_engine, real_client,
                                                                                         monkeypatch):
    from celerp.config import settings
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    monkeypatch.setattr(settings, "cloud_disconnected", True)
    tok = await token(real_engine, boss, a)
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok))

    assert r.status_code == 503, r.text
    assert "Reconnect" in r.json()["detail"] and r.json()["detail"].endswith("Nothing was deleted.")
    assert cloud.calls == []
    assert await snapshot(real_engine) == before


async def test_an_installation_never_connected_to_cloud_resets_without_asking(real_engine, real_client,
                                                                              monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine, credential="")

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert cloud.calls == []
    assert await _companies(real_engine) == {str(b)}
    assert await _closures(real_engine) == []


async def test_a_reset_refused_while_a_payment_settles_succeeds_once_it_has(real_engine, real_client,
                                                                          monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [httpx.Response(409, json={"detail": "payment_settling"})]

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 409
    assert await _companies(real_engine) == {str(a), str(b)}
    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _steps(cloud) == ["prepare", "cancel", "prepare", "finalize"]
    assert await _companies(real_engine) == {str(b)}


RECONNECT = "Reconnect this Stripe account to finish checking payments already in progress. Nothing was deleted."
SETTLING = ("A payment on one of this company's invoices is still being processed. "
            "Try again once it has finished. Nothing was deleted.")


@pytest.mark.parametrize("step, says", [("prepare", SETTLING), ("recovery", SETTLING),
                                        ("prepare", RECONNECT), ("recovery", RECONNECT)])
async def test_a_reset_held_by_a_payment_says_whether_stripe_must_be_reconnected(
        real_engine, real_client, monkeypatch, step, says):
    """A payment still settling only needs time; one Celerp Cloud can check only once
    the withdrawn Stripe account is reconnected says so, and nothing else."""
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    if step == "recovery":
        await _record_restore(real_engine, [a, b])
    cloud.scripted[step] = [httpx.Response(409, json={
        "detail": "reconnect_required" if says == RECONNECT else "payment_settling"})]
    tok = await token(real_engine, boss, a)
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok))

    assert r.status_code == 409 and r.json()["detail"] == says
    assert await snapshot(real_engine) == before


async def test_payments_are_closed_only_after_every_local_check_passes(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    r = await real_client.post(RESET, json={"company_name": "Wrong name"},
                               headers=auth(await token(real_engine, boss, a)))

    assert r.status_code == 422
    assert cloud.calls == []


# ── Refusals after the company is gone ───────────────────────────────────────

async def test_a_payment_arriving_while_the_company_is_deleted_holds_the_closing_until_it_is_recorded(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    async def paid():
        cloud.pay(a)
    cloud.on_prepared = paid

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _steps(cloud) == ["prepare", "finalize"]
    assert cloud.states() == ["prepared"] and not cloud.payments_open(a)
    assert await _companies(real_engine) == {str(b)}
    assert await _closures(real_engine) == [str(a)]  # kept, and tried again

    await reconcile_payments()
    assert _steps(cloud) == ["prepare", "finalize", "finalize"]
    assert cloud.states() == ["prepared"]

    await cloud.deliver()
    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _unmatched(real_engine) == [("pi_late", 107000, "USD", str(a), "doc:gone")]

    await reconcile_payments()
    assert _steps(cloud) == ["prepare", "finalize", "finalize", "finalize"]
    _one_operation(cloud)
    assert cloud.states() == ["retired"] and not cloud.payments_open(a)
    assert await _closures(real_engine) == []


# ── Payments delivered around a reset ────────────────────────────────────────

async def _unmatched(engine) -> list[tuple]:
    async with maker(engine)() as s:
        return [tuple(r) for r in (await s.execute(text(
            "SELECT reference, amount_minor, currency, former_company, document FROM unmatched_payments "
            "ORDER BY received_at"))).all()]


async def _invoice(client, engine, boss, cid, **doc) -> str:
    tok = auth(await token(engine, boss, cid))
    r = await client.post("/docs", json={
        "doc_type": "invoice", "contact_name": "Buyer",
        "line_items": [{"description": "Widget", "quantity": 2, "unit_price": 500.0}],
        "subtotal": 1000.0, "tax": 70.0, "total": 1070.0, "currency": "USD", **doc}, headers=tok)
    eid = r.json()["id"]
    assert (await client.post(f"/docs/{eid}/finalize", headers=tok)).status_code == 200
    return eid


async def _waiting_on_a_lock(engine) -> bool:
    async with engine.connect() as conn:
        return bool(await conn.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock'")))


@pytest.mark.parametrize("moment", ["before", "during", "after"])
async def test_a_payment_delivered_around_a_reset_is_recorded_once_and_the_closing_finishes(
        real_engine, real_client, monkeypatch, moment):
    import asyncio
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _Cloud(monkeypatch, real_engine)
    delivering = None
    if moment == "before":
        cloud.pay(a, invoice, "pi_1")
        await cloud.deliver()
        async with maker(real_engine)() as s:
            state = (await s.execute(text("SELECT state FROM projections WHERE company_id = :c AND entity_id = :e"),
                                     {"c": a, "e": invoice})).scalar_one()
        assert state["status"] == "paid" and [p["reference"] for p in state["payments"]] == ["pi_1"]

    async def during():
        # The reset holds the company; the delivery waits for it to end.
        nonlocal delivering
        cloud.pay(a, invoice, "pi_1")
        delivering = asyncio.create_task(cloud.deliver())
        for _ in range(500):
            if await _waiting_on_a_lock(real_engine):
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the delivery did not wait for the reset")
    if moment == "during":
        cloud.on_prepared = during

    r = await _reset(real_client, real_engine, boss, a)
    assert r.status_code == 200, r.text
    if delivering is not None:
        await delivering
    if moment == "after":
        cloud.pay(a, invoice, "pi_1")
        await cloud.deliver()
    await reconcile_payments()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _unmatched(real_engine) == ([] if moment == "before" else
                                             [("pi_1", 107000, "USD", str(a), invoice)])
    assert cloud.states() == ["retired"]
    assert await _closures(real_engine) == []


async def test_a_payment_delivered_again_is_kept_once(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.pay(b, "doc:gone", "pi_1")
    await cloud.deliver()
    cloud.deliveries[0]["acked"] = False  # the acknowledgement was lost

    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(b), "doc:gone")]


async def test_a_payment_its_document_refuses_is_kept_among_the_unmatched(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.pay(b, "item:1", "pi_1")  # not an invoice

    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(b), "item:1")]


async def test_a_payment_that_cannot_be_recorded_is_not_acknowledged(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.pay(b, "doc:gone", "pi_1")

    async def rename(old, new):
        async with real_engine.begin() as conn:
            await conn.execute(text(f"ALTER TABLE {old} RENAME TO {new}"))
    await rename("unmatched_payments", "unmatched_payments_away")  # recording fails
    try:
        await cloud.deliver()
    finally:
        await rename("unmatched_payments_away", "unmatched_payments")

    assert [d["acked"] for d in cloud.deliveries] == [False]
    await cloud.deliver()
    assert [d["acked"] for d in cloud.deliveries] == [True]


# ── One intake for every online payment ──────────────────────────────────────

async def _paid(engine, entity_id) -> list[tuple]:
    """(reference, amount) of each payment on the invoice, oldest first."""
    async with maker(engine)() as s:
        state = await s.scalar(text("SELECT state FROM projections WHERE entity_id = :e"), {"e": entity_id})
    return [(p.get("reference"), p["amount"]) for p in state.get("payments", []) if p.get("status") != "deleted"]


async def _pay_by_hand(client, engine, boss, cid, entity_id, amount: float):
    r = await client.post(f"/docs/{entity_id}/payment", headers=auth(await token(engine, boss, cid)), json={
        "amount": amount, "payment_date": "2026-10-01", "bank_account": "1110"})
    assert r.status_code == 200, r.text


async def test_a_stripe_payment_for_an_invoice_paid_another_way_is_kept_whole_among_the_unmatched(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _Cloud(monkeypatch, real_engine)
    await _pay_by_hand(real_client, real_engine, boss, a, invoice, 1070.0)

    cloud.pay(a, invoice, "pi_1")
    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _paid(real_engine, invoice) == [(None, 1070.0)]
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(a), invoice)]


async def test_a_stripe_payment_larger_than_what_is_still_owed_is_kept_whole_among_the_unmatched(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _Cloud(monkeypatch, real_engine)
    await _pay_by_hand(real_client, real_engine, boss, a, invoice, 500.0)

    cloud.pay(a, invoice, "pi_1")
    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _paid(real_engine, invoice) == [(None, 500.0)]
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(a), invoice)]


async def test_two_payment_pages_both_paid_record_the_second_among_the_unmatched(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.pay(a, invoice, "pi_first")
    await cloud.deliver()

    cloud.pay(a, invoice, "pi_second")
    await cloud.deliver()

    assert await _paid(real_engine, invoice) == [("pi_first", 1070.0)]
    assert await _unmatched(real_engine) == [("pi_second", 107000, "USD", str(a), invoice)]


async def test_the_customers_return_from_stripe_records_nothing_and_the_delivery_records_the_payment(
        real_engine, real_client, monkeypatch):
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(real_engine)
    await _company_settings(real_engine, a, timezone="Asia/Bangkok")
    invoice, share = await _shared_invoice(real_client, real_engine, boss, a)
    cloud = _Cloud(monkeypatch, real_engine)
    assert (await real_client.get(f"/pay/{share}", follow_redirects=False)).status_code == 303
    month_end = datetime(2025, 10, 31, 17, 30, tzinfo=timezone.utc)  # already November 1 in Bangkok
    cloud.pay(a, invoice, "pi_1", paid_at=month_end)

    r = await real_client.get(f"/pay/{share}/return?session_id=cs_1", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == f"/share/{share}"
    assert cloud.reads == []
    assert await _paid(real_engine, invoice) == [] and await _unmatched(real_engine) == []

    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _payment_dates(real_engine, invoice) == [("pi_1", "2025-11-01", "2025-11-01", "2025-11-01")]
    assert await _unmatched(real_engine) == []


async def test_a_payment_delivered_again_after_it_was_kept_among_the_unmatched_changes_nothing(
        real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    invoice = await _invoice(real_client, real_engine, boss, a)
    cloud = _Cloud(monkeypatch, real_engine)
    await _pay_by_hand(real_client, real_engine, boss, a, invoice, 1070.0)
    cloud.pay(a, invoice, "pi_1")
    await cloud.deliver()
    # The hand-entered payment was a mistake and is removed: the invoice is owed again.
    r = await real_client.delete(f"/docs/{invoice}/payments/0", headers=auth(await token(real_engine, boss, a)))
    assert r.status_code == 200, r.text
    cloud.deliveries[0]["acked"] = False  # the acknowledgement was lost

    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _paid(real_engine, invoice) == []
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(a), invoice)]


@pytest.mark.parametrize("detail", ["generation_stale", "cancelled", "not_prepared"])
async def test_a_finalize_cloud_refuses_for_good_is_forgotten_and_the_payments_stay_closed(
        real_engine, real_client, monkeypatch, detail):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["finalize"] = [httpx.Response(409, json={"detail": detail})]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert cloud.states() == ["prepared"] and not cloud.payments_open(a)
    assert await _closures(real_engine) == []


@pytest.mark.parametrize("detail", ["payment_settling", "payment_unrecorded"])
async def test_a_finalize_refused_while_a_payment_finishes_is_repeated_until_it_finalizes(
        real_engine, real_client, monkeypatch, detail):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["finalize"] = [httpx.Response(409, json={"detail": detail})]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert await _closures(real_engine) == [str(a)]
    assert not cloud.payments_open(a)
    await reconcile_payments()
    assert _steps(cloud) == ["prepare", "finalize", "finalize"]
    _one_operation(cloud)
    assert cloud.states() == ["retired"]
    assert await _closures(real_engine) == []


async def test_a_cancel_cloud_refuses_for_good_is_forgotten_and_the_company_stays(real_engine, real_client,
                                                                                 monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]
    cloud.scripted["cancel"] = [httpx.Response(409, json={"detail": "retired"})]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 503 and r.json()["detail"].endswith("Nothing was deleted.")
    assert not cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}
    assert await _closures(real_engine) == []


async def test_a_lost_cancel_is_reopened_by_the_next_reconciliation_without_a_restart(real_engine, real_client,
                                                                                      monkeypatch):
    import asyncio

    from celerp.services import payments
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]
    cloud.scripted["cancel"] = [httpx.ConnectError("unreachable"), httpx.ConnectError("unreachable")]
    assert (await _reset(real_client, real_engine, boss, a)).status_code == 503
    monkeypatch.setattr(payments, "RECONCILE_INTERVAL_S", 0.01)

    loop = asyncio.create_task(payments.reconcile_payments_loop())
    try:
        for _ in range(500):
            if not await _closures(real_engine):
                break
            await asyncio.sleep(0.01)
    finally:
        loop.cancel()

    assert _steps(cloud)[:4] == ["prepare", "cancel", "cancel", "cancel"]
    assert cloud.payments_open(a)
    assert await _closures(real_engine) == []


# ── The payment generation and System Recovery ───────────────────────────────

async def _recoveries(engine) -> list[tuple[list, int | None]]:
    async with maker(engine)() as s:
        return [(list(r.company_ids), r.generation) for r in (await s.execute(
            text("SELECT company_ids, generation FROM payment_recoveries ORDER BY created_at"))).all()]


async def _record_restore(engine, company_ids, generation=None) -> str:
    recovery_id = str(uuid.uuid4())
    async with maker(engine)() as s:
        await s.execute(text("INSERT INTO payment_recoveries (recovery_id, company_ids, generation) "
                             "VALUES (:r, CAST(:c AS json), :g)"),
                        {"r": recovery_id, "c": json.dumps(sorted(map(str, company_ids))), "g": generation})
        await s.commit()
    return recovery_id


async def test_each_step_carries_the_installations_payment_generation(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.generation = 2
    await _record_restore(real_engine, [a, b], generation=1)
    await _record_restore(real_engine, [a, b], generation=2)

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _steps(cloud) == ["prepare", "finalize"]
    assert cloud.generations == [2, 2]
    assert cloud.states() == ["retired"]


async def test_a_restore_cloud_has_not_confirmed_refuses_the_reset_until_it_has(real_engine, real_client,
                                                                               monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    restore = await _record_restore(real_engine, [a, b])
    cloud.scripted["recovery"] = [httpx.ConnectError("unreachable")]
    tok = await token(real_engine, boss, a)
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok))

    assert r.status_code == 503, r.text
    assert "could not confirm" in r.json()["detail"] and r.json()["detail"].endswith("Nothing was deleted.")
    assert cloud.calls == [("recovery", restore)]
    assert await snapshot(real_engine) == before

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _steps(cloud) == ["recovery", "recovery", "prepare", "finalize"]
    assert cloud.generations == [1, 1]
    assert await _recoveries(real_engine) == [(sorted([str(a), str(b)]), 1)]


@pytest.mark.parametrize("answer", [
    httpx.Response(200, json={"recovery_id": "another", "generation": 1}),
    httpx.Response(200, json={"generation": True}),
    httpx.Response(409, json={"detail": "Invalid recovery"}),
], ids=["other-recovery", "no-generation", "refused"])
async def test_a_restore_cloud_does_not_confirm_stays_unconfirmed(real_engine, real_client, monkeypatch, answer):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    await _record_restore(real_engine, [a, b])
    cloud.scripted["recovery"] = [answer]

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 503
    assert _steps(cloud) == ["recovery"]
    assert await _recoveries(real_engine) == [(sorted([str(a), str(b)]), None)]


async def test_a_closing_from_before_a_restore_is_reopened_and_cannot_finish(real_engine, real_client,
                                                                            monkeypatch):
    from celerp.services import payments
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    settle = payments.settle_company_closure

    async def crashed(operation_id):
        return False
    monkeypatch.setattr(payments, "settle_company_closure", crashed)
    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
    monkeypatch.setattr(payments, "settle_company_closure", settle)
    # A restore that did not bring the company back: its closing was prepared before.
    await _record_restore(real_engine, [b])

    await payments.reconcile_payments()

    assert _steps(cloud) == ["prepare", "recovery", "finalize"]
    assert cloud.states() == ["prepared"] and not cloud.payments_open(a)
    assert await _closures(real_engine) == []


def _system_recovery(tmp_path, monkeypatch):
    from test_system_recovery_exact import _Recovery, _set_enabled
    _set_enabled(["celerp-inventory"])
    return _Recovery(tmp_path, monkeypatch, real_database=True)


async def test_a_restore_that_brings_a_reset_company_back_reopens_its_payments(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    source = await backup_export.export_full()
    try:
        assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
        assert cloud.states() == ["retired"] and not cloud.payments_open(a)

        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)

    assert result.ok is True, result.error
    assert await _companies(real_engine) == {str(a), str(b)}
    assert _steps(cloud) == ["prepare", "finalize", "recovery"]
    assert cloud.recoveries[cloud.calls[-1][1]] == (1, sorted([str(a), str(b)]))
    assert cloud.payments_open(a)
    assert await _recoveries(real_engine) == [(sorted([str(a), str(b)]), 1)]

    # The restored company can be reset again, under the new generation.
    boss_token = await token(real_engine, boss, a)
    r = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(boss_token))
    assert r.status_code == 200, r.text
    assert cloud.generations[-2:] == [1, 1]


async def test_a_restore_that_does_not_bring_a_reset_company_back_keeps_it_closed(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
    source = await backup_export.export_full()
    try:
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)

    assert result.ok is True, result.error
    assert await _companies(real_engine) == {str(b)}
    assert _steps(cloud) == ["prepare", "finalize", "recovery"]
    assert cloud.recoveries[cloud.calls[-1][1]] == (1, [str(b)])
    assert cloud.states() == ["retired"] and not cloud.payments_open(a)


async def test_a_restore_cloud_cannot_learn_of_yet_is_reported_by_the_next_reconciliation(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    from celerp.services import backup_export, backup_import, payments
    _system_recovery(tmp_path, monkeypatch)
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    source = await backup_export.export_full()
    try:
        assert (await _reset(real_client, real_engine, boss, a)).status_code == 200
        cloud.scripted["recovery"] = [httpx.ConnectError("unreachable")]
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)

    assert result.ok is True, result.error
    assert not cloud.payments_open(a)
    assert await _recoveries(real_engine) == [(sorted([str(a), str(b)]), None)]

    await payments.reconcile_payments()  # the next start, or the next interval

    assert cloud.payments_open(a)
    assert await _recoveries(real_engine) == [(sorted([str(a), str(b)]), 1)]


# ── Payments a System Recovery restore lost ──────────────────────────────────

async def _references(engine, entity_id) -> list[str]:
    async with maker(engine)() as s:
        state = await s.scalar(text("SELECT state FROM projections WHERE entity_id = :e"), {"e": entity_id})
    return [p["reference"] for p in state.get("payments", []) if p.get("status") != "deleted"]


def _payments_on(monkeypatch) -> None:
    from celerp.config import settings
    monkeypatch.setattr(settings, "celerp_public_url", "https://harbor.celerp.com")
    monkeypatch.setattr("celerp.services.payments.payments_enabled", lambda: True)


async def _shared_invoice(client, engine, boss, cid, **doc) -> tuple[str, str]:
    eid = await _invoice(client, engine, boss, cid, **doc)
    r = await client.post(f"/docs/{eid}/share", headers=auth(await token(engine, boss, cid)))
    return eid, r.json()["token"]


async def test_every_payment_a_restore_may_have_lost_is_recorded_again_before_a_new_payment_opens(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    eid, share = await _shared_invoice(real_client, real_engine, boss, a)

    # Saturday: a deposit, recorded before the backup.
    cloud.pay(a, eid, "pi_saturday", amount_minor=7000)
    await cloud.deliver()
    # Sunday: the backup.
    source = await backup_export.export_full()
    try:
        assert "snapshot_started_at" not in (await backup_export.archive_meta())
        # Monday: the customer pays half the balance.
        cloud.pay(a, eid, "pi_monday", amount_minor=50000)
        await cloud.deliver()
        assert await _references(real_engine, eid) == ["pi_saturday", "pi_monday"]
        # Friday: Sunday's backup is restored.
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)

    assert result.ok is True, result.error
    assert await _references(real_engine, eid) == ["pi_saturday"]
    # Every payment ever recorded is delivered again, whatever its time.
    assert sorted(d["reference"] for d in cloud.deliveries if d.get("replay")) == ["pi_monday", "pi_saturday"]

    # Until they are recorded again, no new payment opens.
    r = await real_client.get(f"/pay/{share}", follow_redirects=False)
    assert r.status_code == 409 and r.json()["detail"] == PAUSED
    assert cloud.checkouts == []

    await cloud.deliver()
    assert all(d["acked"] for d in cloud.deliveries) and len(cloud.deliveries) == 4
    assert await _references(real_engine, eid) == ["pi_saturday", "pi_monday"]  # each once
    assert await _unmatched(real_engine) == []

    r = await real_client.get(f"/pay/{share}", follow_redirects=False)
    assert r.status_code == 303 and cloud.checkouts == [(str(a), 1, 200)]


async def _company_settings(engine, cid, **changes) -> None:
    async with maker(engine)() as s:
        settings = await s.scalar(text("SELECT settings FROM companies WHERE id = :c"), {"c": cid}) or {}
        await s.execute(text("UPDATE companies SET settings = CAST(:s AS json) WHERE id = :c"),
                        {"s": json.dumps({**settings, **changes}), "c": cid})
        await s.commit()


async def _payment_dates(engine, entity_id) -> list[tuple[str, str, str]]:
    """(reference, payment date, its journal entry's date) of each payment on the
    invoice, and the date its doc.payment.received event carries."""
    async with maker(engine)() as s:
        state = await s.scalar(text("SELECT state FROM projections WHERE entity_id = :e"), {"e": entity_id})
        events = {e["reference"]: e["payment_date"] for e in (await s.scalars(text(
            "SELECT data FROM ledger WHERE entity_id = :e AND event_type = 'doc.payment.received'"),
            {"e": entity_id})).all()}
        journal = {}
        for je_id, je in (await s.execute(text(
                "SELECT entity_id, state FROM projections WHERE entity_id LIKE :j"),
                {"j": f"je:auto:{entity_id}:pay:%"})).all():
            journal[je_id] = str(je.get("ts"))[:10]
    paid = [p for p in state.get("payments", []) if p.get("status") != "deleted"]
    return [(p["reference"], p["payment_date"], events[p["reference"]],
             journal[f"je:auto:{entity_id}:pay:{p['index']}"]) for p in paid]


# October 3 in Bangkok; the backup that loses it is restored weeks later.
_OCTOBER_3 = datetime(2025, 10, 3, 3, 0, tzinfo=timezone.utc)


async def _posting(engine, entity_id) -> list[tuple]:
    """How each payment on the invoice posted: its reference, day, bank account and
    rate, the day and bank account its doc.payment.received event carries, and its
    journal entry's day and lines."""
    async with maker(engine)() as s:
        state = await s.scalar(text("SELECT state FROM projections WHERE entity_id = :e"), {"e": entity_id})
        events = {e["reference"]: e for e in (await s.scalars(text(
            "SELECT data FROM ledger WHERE entity_id = :e AND event_type = 'doc.payment.received'"),
            {"e": entity_id})).all()}
        journal = dict((await s.execute(text("SELECT entity_id, state FROM projections WHERE entity_id LIKE :j"),
                                        {"j": f"je:auto:{entity_id}:pay:%"})).all())
    posted = []
    for p in [p for p in state.get("payments", []) if p.get("status") != "deleted"]:
        event, je = events[p["reference"]], journal[f"je:auto:{entity_id}:pay:{p['index']}"]
        posted.append((p["reference"], p["payment_date"], p["bank_account"], p.get("conversion_rate"),
                       event["payment_date"], event["bank_account"], str(je.get("ts"))[:10],
                       sorted((e["account"], e["debit"], e["credit"]) for e in je["entries"])))
    return posted


async def _restore_losing_a_payment(tmp_path, monkeypatch, engine, client, paid_at: datetime, *,
                                    opened: dict, account: str | None = None, restored: dict | None = None,
                                    company_settings: dict | None = None, **invoice):
    """After the last backup, the company's settings change to *opened* (and *account*
    is added to its chart), a customer opens the invoice's payment page and pays. A
    System Recovery restore of the backup loses the payment; the settings then change
    to *restored*, and Celerp Cloud delivers the payment again. Returns how it first
    posted."""
    from celerp.services import backup_export, backup_import
    _system_recovery(tmp_path, monkeypatch)
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(engine)
    if company_settings:
        await _company_settings(engine, a, **company_settings)
    cloud = _Cloud(monkeypatch, engine)
    eid, share = await _shared_invoice(client, engine, boss, a, **invoice)
    source = await backup_export.export_full()
    try:
        await _company_settings(engine, a, **opened)
        if account:
            await _add_account(engine, a, account)
        assert (await client.get(f"/pay/{share}", follow_redirects=False)).status_code == 303
        cloud.pay(a, eid, "pi_paid", amount_minor=50000, paid_at=paid_at)
        await cloud.deliver()
        first = await _posting(engine, eid)
        result = await backup_import.run_recovery(source)
    finally:
        source.unlink(missing_ok=True)
    assert result.ok is True, result.error
    assert await _references(engine, eid) == []
    if restored:
        await _company_settings(engine, a, **restored)
    await cloud.deliver()
    assert all(d["acked"] for d in cloud.deliveries)
    return boss, a, eid, first


async def _add_account(engine, cid, code: str) -> None:
    from celerp_accounting.models import Account
    async with maker(engine)() as s:
        s.add(Account(id=uuid.uuid4(), company_id=cid, code=code, name="Online payments clearing",
                      account_type="asset", parent_code="1110"))
        await s.commit()


async def test_a_payment_recorded_again_after_a_restore_keeps_the_day_it_was_paid(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    boss, a, eid, first = await _restore_losing_a_payment(
        tmp_path, monkeypatch, real_engine, real_client, _OCTOBER_3, opened={"timezone": "Asia/Bangkok"})
    # The payment, its event and its journal entry all stay on October 3.
    assert await _payment_dates(real_engine, eid) == [("pi_paid", "2025-10-03", "2025-10-03", "2025-10-03")]


async def test_a_payment_recorded_again_after_a_restore_posts_as_its_page_opened(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    """The company keeps its books in baht and invoices in dollars. After the backup it
    moves online payments to another bank account and its calendar to Bangkok; a
    customer then opens the payment page and pays at the end of October, already
    November 1 in Bangkok. The restore brings back the old settings and loses the
    payment; recorded again, it posts on the same day, to the same account, at the same
    rates, exactly as it first did."""
    month_end = datetime(2025, 10, 31, 17, 30, tzinfo=timezone.utc)
    boss, a, eid, first = await _restore_losing_a_payment(
        tmp_path, monkeypatch, real_engine, real_client, month_end,
        company_settings={"currency": "THB", "timezone": "America/New_York"},
        opened={"timezone": "Asia/Bangkok", "stripe_deposit_account": "1111"},
        restored={"currency": "THB"}, conversion_rate=35.125)
    async with maker(real_engine)() as s:
        settings = await s.scalar(text("SELECT settings FROM companies WHERE id = :c"), {"c": a})
    assert settings.get("timezone") == "America/New_York" and "stripe_deposit_account" not in settings

    assert first == [("pi_paid", "2025-11-01", "1111", 35.125, "2025-11-01", "1111", "2025-11-01",
                      [("1111", 17562.5, 0.0), ("1120", 0.0, 17562.5)])]
    assert await _posting(real_engine, eid) == first


async def test_a_payment_whose_bank_account_a_restore_removed_is_kept_among_the_unmatched(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    """The account online payments clear to was added after the backup: the payment is
    never posted to another account in its place."""
    boss, a, eid, first = await _restore_losing_a_payment(
        tmp_path, monkeypatch, real_engine, real_client, _OCTOBER_3,
        opened={"stripe_deposit_account": "1119"}, account="1119")
    assert [p[2] for p in first] == ["1119"]
    assert await _references(real_engine, eid) == []
    assert await _unmatched(real_engine) == [("pi_paid", 50000, "USD", str(a), eid)]
    async with maker(real_engine)() as s:
        assert await s.scalar(text("SELECT paid_at FROM unmatched_payments")) == _OCTOBER_3


@pytest.mark.parametrize("books", [
    None,
    {k: v for k, v in BOOKS.items() if k != "deposit_account"},
    {**BOOKS, "timezone": "Mars/Olympus"},
    {**BOOKS, "rate": "0"},
    {**BOOKS, "deposit_account": "9999"},
], ids=["none", "no-account", "bad-timezone", "bad-rate", "unknown-account"])
async def test_a_payment_without_usable_books_from_its_page_is_kept_among_the_unmatched(
        real_engine, real_client, monkeypatch, books):
    """A payment whose page did not record the books it opened with (or recorded books
    this company cannot post to) is never recorded from today's settings instead."""
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    eid = await _invoice(real_client, real_engine, boss, a)
    cloud.pay(a, eid, "pi_1", paid_at=_OCTOBER_3, books=books)

    await cloud.deliver()

    assert [d["acked"] for d in cloud.deliveries] == [True]
    assert await _paid(real_engine, eid) == []
    assert await _unmatched(real_engine) == [("pi_1", 107000, "USD", str(a), eid)]


@pytest.mark.parametrize("paid_at,timezone_name,business_day", [
    # Already the next morning in Bangkok.
    (datetime(2025, 10, 3, 18, 30, tzinfo=timezone.utc), "Asia/Bangkok", "2025-10-04"),
    # Still the previous evening in New York.
    (datetime(2025, 10, 4, 2, 0, tzinfo=timezone.utc), "America/New_York", "2025-10-03"),
    # The last evening of the month in New York, already the next month in Bangkok.
    (datetime(2025, 10, 31, 23, 30, tzinfo=timezone.utc), "America/New_York", "2025-10-31"),
    (datetime(2025, 10, 31, 23, 30, tzinfo=timezone.utc), "Asia/Bangkok", "2025-11-01"),
])
async def test_an_online_payment_is_dated_on_the_companys_own_calendar(
        real_engine, real_client, monkeypatch, paid_at, timezone_name, business_day):
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(real_engine)
    await _company_settings(real_engine, a, timezone=timezone_name)
    cloud = _Cloud(monkeypatch, real_engine)
    eid, share = await _shared_invoice(real_client, real_engine, boss, a)
    assert (await real_client.get(f"/pay/{share}", follow_redirects=False)).status_code == 303
    cloud.pay(a, eid, "pi_edge", amount_minor=50000, paid_at=paid_at)
    await cloud.deliver()
    assert await _payment_dates(real_engine, eid) == [("pi_edge", business_day, business_day, business_day)]


async def test_a_payment_recorded_again_into_a_locked_period_is_refused_like_any_other(
        tmp_path, monkeypatch, code_config, real_engine, real_client):
    """The books were locked through October after the payment: recording it again
    on October 3 is refused, as recording any payment on October 3 now is. It is kept
    whole among the unmatched payments with the day it was paid, never moved to an
    open day."""
    boss, a, eid, first = await _restore_losing_a_payment(
        tmp_path, monkeypatch, real_engine, real_client, _OCTOBER_3,
        opened={"timezone": "Asia/Bangkok"}, restored={"lock_date": "2025-10-31"})
    assert await _references(real_engine, eid) == []
    assert await _unmatched(real_engine) == [("pi_paid", 50000, "USD", str(a), eid)]
    async with maker(real_engine)() as s:
        assert await s.scalar(text("SELECT paid_at FROM unmatched_payments")) == _OCTOBER_3
    r = await real_client.post(f"/docs/{eid}/payment", headers=auth(await token(real_engine, boss, a)),
                               json={"amount": 500.0, "payment_date": "2025-10-03", "bank_account": "1110"})
    assert r.status_code == 422 and r.json()["detail"].startswith("Period is locked through 2025-10-31")


@pytest.mark.parametrize("refusal", ["generation_stale", "recovery_pending"])
async def test_a_payment_cloud_refuses_until_a_restore_is_confirmed_reads_as_paused(
        real_engine, real_client, monkeypatch, refusal):
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    eid, share = await _shared_invoice(real_client, real_engine, boss, a)
    # A restore this installation has not heard of yet (another restore of its data).
    cloud.generation = 1
    cloud.recoveries["elsewhere"] = (1, [])
    if refusal == "generation_stale":
        cloud.confirmed.add("elsewhere")

    r = await real_client.get(f"/pay/{share}", follow_redirects=False)

    assert r.status_code == 409 and r.json()["detail"] == PAUSED
    assert cloud.checkouts == [(str(a), 0, 409)]


@pytest.mark.parametrize("answer", [httpx.ConnectError("unreachable"), NO_ANSWER,
                                    httpx.Response(409, json={"detail": "reconnect_required"})],
                         ids=["unreachable", "no-answer", "reconnect"])
async def test_new_payments_wait_until_cloud_confirms_a_restore(real_engine, real_client, monkeypatch, answer):
    _payments_on(monkeypatch)
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    eid, share = await _shared_invoice(real_client, real_engine, boss, a)
    await _record_restore(real_engine, [a, b])
    cloud.scripted["recovery"] = [answer]

    r = await real_client.get(f"/pay/{share}", follow_redirects=False)

    assert r.status_code == 409 and r.json()["detail"] == PAUSED
    assert cloud.checkouts == []

    r = await real_client.get(f"/pay/{share}", follow_redirects=False)
    assert r.status_code == 303 and cloud.checkouts == [(str(a), 1, 200)]
