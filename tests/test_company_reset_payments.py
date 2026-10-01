# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company connected to Celerp Cloud has its online invoice payments closed before it
is reset, so no customer can pay it after it is gone and no payment can arrive for it.
When Celerp Cloud cannot confirm the payments are closed, nothing is deleted."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text

from company_backup_support import company, owner, snapshot, token
from migration_support import auth, maker, real_client, real_engine  # noqa: F401

pytestmark = pytest.mark.asyncio

RESET = "/companies/me/reset"
RETIRE = "/billing/connect/companies/retire"
NAME = "Harbor Goods Ltd"


class _Cloud:
    """Celerp Cloud as the installation reaches it, answering the close request."""

    def __init__(self, monkeypatch, engine, *, credential: str = "cloud-credential") -> None:
        from celerp.services import cloud_entitlement
        self.engine = engine
        self.answers: list = []
        self.calls: list[tuple[str, str, dict]] = []
        self.company_present: list[bool] = []

        async def stored_api_key() -> str:
            return credential

        monkeypatch.setattr(cloud_entitlement, "stored_api_key", stored_api_key)
        monkeypatch.setattr(cloud_entitlement, "authenticated_request", self._request)

    def closed(self, company_id) -> httpx.Response:
        return httpx.Response(200, json={"retired": True, "company_id": str(company_id)})

    async def _request(self, method, path, *, total_s=None, json=None, params=None, api_key=None):
        self.calls.append((method, path, json))
        async with maker(self.engine)() as s:
            self.company_present.append(bool(await s.scalar(
                text("SELECT count(*) FROM companies WHERE id = :c"), {"c": json["company_id"]})))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


async def _harbor(engine):
    boss = await owner(engine)
    a = await company(engine, boss, NAME, "alpha")
    b = await company(engine, boss, "Hillside Supply Co", "bravo")
    return boss, a, b


async def _companies(engine) -> set[str]:
    async with maker(engine)() as s:
        return {str(c) for c in (await s.scalars(text("SELECT id FROM companies"))).all()}


async def _reset(client, engine, boss, cid):
    return await client.post(RESET, json={"company_name": NAME}, headers=auth(await token(engine, boss, cid)))


async def test_reset_closes_the_companys_online_payments_before_deleting_it(real_engine, real_client,
                                                                            monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.answers = [cloud.closed(a)]

    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert cloud.calls == [("POST", RETIRE, {"company_id": str(a)})]
    assert cloud.company_present == [True]  # asked before anything was deleted
    assert await _companies(real_engine) == {str(b)}


@pytest.mark.parametrize("answer, status, says", [
    (None, 503, "could not confirm"),
    (httpx.ConnectError("unreachable"), 503, "could not confirm"),
    (httpx.Response(502, json={"detail": "Could not confirm"}), 503, "could not confirm"),
    (httpx.Response(500, text="Internal Server Error"), 503, "could not confirm"),
    (httpx.Response(409, json={"detail": "payment_settling"}), 409, "still being processed"),
    (httpx.Response(409, json={"detail": "payment_unrecorded"}), 409, "has not reached Celerp"),
    (httpx.Response(409, json={"detail": "something else"}), 503, "could not confirm"),
    (httpx.Response(200, json={"retired": True, "company_id": "another-company"}), 503, "could not confirm"),
    (httpx.Response(200, json={"retired": False}), 503, "could not confirm"),
    (httpx.Response(200, text="<html>proxy</html>"), 503, "could not confirm"),
], ids=["no-answer", "unreachable", "stripe-unconfirmed", "server-error", "settling", "unrecorded",
        "unknown-refusal", "other-company", "not-closed", "not-json"])
async def test_nothing_is_deleted_unless_cloud_confirms_the_payments_are_closed(
        real_engine, real_client, monkeypatch, answer, status, says):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.answers = [answer]
    tok = await token(real_engine, boss, a)
    before = await snapshot(real_engine)

    r = await real_client.post(RESET, json={"company_name": NAME}, headers=auth(tok))

    assert r.status_code == status, r.text
    assert says in r.json()["detail"] and r.json()["detail"].endswith("Nothing was deleted.")
    assert await snapshot(real_engine) == before


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


async def test_a_reset_refused_while_a_payment_settles_succeeds_once_it_has(real_engine, real_client,
                                                                          monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)
    cloud.answers = [httpx.Response(409, json={"detail": "payment_settling"}), cloud.closed(a)]

    assert (await _reset(real_client, real_engine, boss, a)).status_code == 409
    assert await _companies(real_engine) == {str(a), str(b)}
    r = await _reset(real_client, real_engine, boss, a)

    assert r.status_code == 200, r.text
    assert [call[2] for call in cloud.calls] == [{"company_id": str(a)}] * 2
    assert await _companies(real_engine) == {str(b)}


async def test_payments_are_closed_only_after_every_local_check_passes(real_engine, real_client, monkeypatch):
    boss, a, b = await _harbor(real_engine)
    cloud = _Cloud(monkeypatch, real_engine)

    r = await real_client.post(RESET, json={"company_name": "Wrong name"},
                               headers=auth(await token(real_engine, boss, a)))

    assert r.status_code == 422
    assert cloud.calls == []
