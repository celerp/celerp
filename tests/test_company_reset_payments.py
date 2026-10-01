# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company connected to Celerp Cloud has its online invoice payments frozen at Cloud
before it is reset. Once the reset has committed, Cloud closes them for good; when it
does not commit, Cloud reopens them, so a company that stays keeps taking payments.
When Celerp Cloud cannot confirm the freeze, nothing is deleted. A reset interrupted
part way is settled the same way on the next start."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from company_backup_support import company, owner, snapshot, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
CLOSURE = "/billing/connect/companies/retire/"
NAME = "Harbor Goods Ltd"
LOST = object()  # Cloud takes the step, but its answer never arrives
NO_ANSWER = object()  # the request returns nothing


class _Crash(BaseException):
    """The process stops."""


class _Cloud:
    """Celerp Cloud as the installation reaches it: each closing request moves from
    frozen to retired or cancelled, as Cloud does. A scripted answer replaces the next
    answer to one step."""

    def __init__(self, monkeypatch, engine, *, credential: str = "cloud-credential") -> None:
        from celerp.services import cloud_entitlement
        self.engine = engine
        self.ops: dict[str, tuple[str, str]] = {}  # operation -> (company, state)
        self.scripted: dict[str, list] = {"prepare": [], "finalize": [], "cancel": []}
        self.calls: list[tuple[str, str]] = []  # (step, operation)
        self.company_present: list[bool] = []
        self.on_prepared = None

        async def stored_api_key() -> str:
            return credential

        monkeypatch.setattr(cloud_entitlement, "stored_api_key", stored_api_key)
        monkeypatch.setattr(cloud_entitlement, "authenticated_request", self._request)

    def payments_open(self, company_id) -> bool:
        return not any(c == str(company_id) and s != "cancelled" for c, s in self.ops.values())

    def states(self) -> list[str]:
        return sorted(s for _, s in self.ops.values())

    def _take(self, step: str, company_id: str, op: str) -> httpx.Response:
        state = self.ops.get(op, (company_id, None))[1]
        if step == "prepare":
            if state == "cancelled":
                return httpx.Response(409, json={"detail": "cancelled"})
            if state is None:
                if any(c == company_id and s == "prepared" for c, s in self.ops.values()):
                    return httpx.Response(409, json={"detail": "closure_pending"})
                state = "prepared"
        elif step == "finalize":
            state = "retired"
        else:
            if state == "retired":
                return httpx.Response(409, json={"detail": "retired"})
            state = "cancelled"
        self.ops[op] = (company_id, state)
        return httpx.Response(200, json={"company_id": company_id, "operation_id": op, "state": state})

    async def _request(self, method, path, *, total_s=None, json=None, params=None, api_key=None):
        assert method == "POST" and path.startswith(CLOSURE)
        step = path.removeprefix(CLOSURE)
        self.calls.append((step, json["operation_id"]))
        async with maker(self.engine)() as s:
            self.company_present.append(bool(await s.scalar(
                text("SELECT count(*) FROM companies WHERE id = :c"), {"c": json["company_id"]})))
        answer = self.scripted[step].pop(0) if self.scripted[step] else None
        if answer is NO_ANSWER:
            return None
        if answer is not None and answer is not LOST:
            if isinstance(answer, Exception):
                raise answer
            return answer
        response = self._take(step, json["company_id"], json["operation_id"])
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


async def test_a_reset_freezes_the_payments_then_closes_them_once_the_company_is_gone(real_engine, real_client,
                                                                                    monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert _steps(cloud) == ["prepare", "finalize"]
    _one_operation(cloud)
    assert cloud.company_present == [True, False]  # frozen before anything was deleted
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
        "unknown-refusal", "other-company", "not-frozen", "not-json"])
async def test_nothing_is_deleted_unless_cloud_confirms_the_payments_are_frozen(
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


async def test_a_freeze_whose_answer_is_lost_is_reopened_and_nothing_is_deleted(real_engine, real_client,
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


async def test_a_lost_freeze_that_cannot_be_reopened_stays_frozen_until_the_next_start(real_engine, real_client,
                                                                                      monkeypatch):
    from celerp.services.payments import settle_company_closures
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["prepare"] = [LOST]
    cloud.scripted["cancel"] = [httpx.ConnectError("unreachable")]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 503
    assert cloud.states() == ["prepared"] and not cloud.payments_open(a)
    assert await _closures(real_engine) == [str(a)]

    await settle_company_closures()  # the next start
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


def _commit_fails_after_the_freeze(monkeypatch, cloud, *, landed: bool):
    """The reset's commit, the first one after Cloud froze the payments, reports a
    failure: before reaching the database, or after it committed (the answer lost)."""
    real = AsyncSession.commit
    armed = []

    async def frozen():
        armed.append(True)
    cloud.on_prepared = frozen

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


async def test_a_reset_whose_commit_fails_after_the_freeze_reopens_the_payments(real_engine, real_client,
                                                                               monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    _commit_fails_after_the_freeze(monkeypatch, cloud, landed=False)

    assert await _status(_reset(real_client, real_engine, boss, a)) != 200

    assert _steps(cloud) == ["prepare", "cancel"]
    assert cloud.payments_open(a)
    assert await _companies(real_engine) == {str(a), str(b)}
    assert await _closures(real_engine) == []


async def test_a_commit_that_landed_but_reported_failure_closes_the_payments_for_good(real_engine, real_client,
                                                                                     monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    _commit_fails_after_the_freeze(monkeypatch, cloud, landed=True)

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
    await payments.settle_company_closures()  # the next start
    assert _steps(cloud) == ["prepare", "finalize"]
    _one_operation(cloud)
    assert cloud.states() == ["retired"]
    assert await _closures(real_engine) == []

    await payments.settle_company_closures()  # settling again is a no-op
    assert _steps(cloud) == ["prepare", "finalize"]


async def test_a_finalize_that_fails_is_repeated_on_the_next_start(real_engine, real_client, monkeypatch):
    from celerp.services.payments import settle_company_closures
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.scripted["finalize"] = [LOST]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert await _closures(real_engine) == [str(a)]
    await settle_company_closures()
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
    await payments.settle_company_closures()  # the next start
    assert _steps(cloud) == ["prepare", "cancel"]
    assert cloud.payments_open(a)
    assert await _closures(real_engine) == []


async def test_a_reset_retried_after_a_crash_reopens_the_stale_request_first(real_engine, real_client,
                                                                            monkeypatch):
    from celerp.services import payments
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


async def test_payments_are_frozen_only_after_every_local_check_passes(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    r = await real_client.post(RESET, json={"company_name": "Wrong name"},
                               headers=auth(await token(real_engine, boss, a)))

    assert r.status_code == 422
    assert cloud.calls == []
