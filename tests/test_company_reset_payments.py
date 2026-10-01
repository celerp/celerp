# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company connected to Celerp Cloud has its online invoice payments closed at Cloud
before it is reset. Once the reset has committed, Cloud closes them for good; when it
does not commit, Cloud reopens them, so a company that stays keeps taking payments.
When Celerp Cloud cannot confirm, nothing is deleted. A reset interrupted part way is
settled the same way on the next start or the next reconciliation. A System Recovery
restore is reported to Cloud, which reopens the payments of the companies it brought
back; until Cloud confirms it, no company can be reset."""

from __future__ import annotations

import json
import uuid

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
NAME = "Harbor Goods Ltd"
LOST = object()  # Cloud takes the step, but its answer never arrives
NO_ANSWER = object()  # the request returns nothing


class _Crash(BaseException):
    """The process stops."""


class _Cloud:
    """Celerp Cloud as the installation reaches it: each closing request moves from
    prepared to retired or cancelled, as Cloud does, under the installation's payment
    generation, which each reported System Recovery restore advances. A scripted
    answer replaces the next answer to one step."""

    def __init__(self, monkeypatch, engine, *, credential: str = "cloud-credential") -> None:
        from celerp.services import cloud_entitlement
        self.engine = engine
        self.ops: dict[str, dict] = {}  # operation -> company, state, generation, paid
        self.generation = 0
        self.recoveries: dict[str, tuple[int, list[str]]] = {}
        self.scripted: dict[str, list] = {"prepare": [], "finalize": [], "cancel": [], "recovery": []}
        self.calls: list[tuple[str, str]] = []  # (step, operation or recovery)
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

    def pay(self, company_id) -> None:
        """A customer's payment reaches the company while its closing is prepared."""
        for o in self.ops.values():
            if o["company"] == str(company_id) and o["state"] == "prepared":
                o["paid"] = True

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
                row = self.ops[op] = {"company": company_id, "state": "prepared",
                                      "generation": generation, "paid": False}
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
                if row["paid"]:
                    return self._refuse("payment_received")
                row["state"] = "retired"
        else:
            if row is None:
                row = self.ops[op] = {"company": company_id, "state": "cancelled",
                                      "generation": generation, "paid": False}
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
        return httpx.Response(200, json={"recovery_id": recovery_id, "generation": self.recoveries[recovery_id][0]})

    async def _request(self, method, path, *, total_s=None, json=None, params=None, api_key=None):
        assert method == "POST"
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
            response = self._recover(key, json["company_ids"])
        else:
            response = self._take(step, json["company_id"], key, json["generation"])
        if answer is LOST:
            raise httpx.ReadTimeout("no answer")
        if step == "prepare" and response.status_code == 200 and self.on_prepared:
            await self.on_prepared()
        return response


async def _harbor(engine):
    boss = await owner(engine)
    a = await company(engine, boss, NAME, "alpha")
    b = await company(engine, boss, "Hillside Supply Co", "bravo")
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
    (httpx.Response(409, json={"detail": "payment_settling"}), 409, "still being processed"),
    (httpx.Response(409, json={"detail": "payment_unrecorded"}), 409, "has not reached Celerp"),
    (httpx.Response(409, json={"detail": "something else"}), 503, "could not confirm"),
    (httpx.Response(200, json={"company_id": "another-company", "operation_id": "x", "state": "prepared"}),
     503, "could not confirm"),
    (httpx.Response(200, json={"state": "cancelled"}), 503, "could not confirm"),
    (httpx.Response(200, text="<html>proxy</html>"), 503, "could not confirm"),
], ids=["no-answer", "unreachable", "stripe-unconfirmed", "server-error", "settling", "unrecorded",
        "unknown-refusal", "other-company", "not-prepared", "not-json"])
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


async def test_payments_are_closed_only_after_every_local_check_passes(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    r = await real_client.post(RESET, json={"company_name": "Wrong name"},
                               headers=auth(await token(real_engine, boss, a)))

    assert r.status_code == 422
    assert cloud.calls == []


# ── Refusals after the company is gone ───────────────────────────────────────

async def test_a_payment_arriving_while_the_company_is_deleted_keeps_its_payments_closed(real_engine, real_client,
                                                                                         monkeypatch):
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
    assert await _closures(real_engine) == []  # it can never finalize, so it is not retried

    await reconcile_payments()
    assert _steps(cloud) == ["prepare", "finalize"]


@pytest.mark.parametrize("detail", ["payment_received", "generation_stale", "cancelled", "not_prepared"])
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
