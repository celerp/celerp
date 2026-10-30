# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Two connections meeting on the same company and item never deadlock.

Each test runs one writer up to the point between its two locks, starts the other writer
until it waits, then lets the first go on. When both take their locks in rank order
(the company first, then the shop identity namespace, then item rows) the second simply
waits for the first; a writer that takes a later lock before the company makes PostgreSQL
abort one of them with a deadlock."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from test_import_preflight_invariants import _factory, _seed_member_with_item, _until_blocked_or_done

pytestmark = pytest.mark.asyncio

_PLATFORM = "woocommerce"


async def _paused(real, entered: asyncio.Event, go: asyncio.Event):
    async def wrapper(*args, **kwargs):
        entered.set()
        await go.wait()
        return await real(*args, **kwargs)
    return wrapper


async def _linked_item(factory):
    import celerp_inventory.services as svc

    company_id, user_id, entity_id = await _seed_member_with_item(factory)
    async with factory() as s:
        await svc.set_external_link(s, company_id, entity_id, _PLATFORM,
                                    {"product_id": "77", "variation_id": None, "sku": "HOLD-1",
                                     "sync_enabled": True})
        await s.commit()
    return company_id, user_id, entity_id


async def _both_finish(first: asyncio.Task, second: asyncio.Task) -> None:
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=5)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, f"one writer failed instead of waiting: {errors!r}"


async def test_a_store_stop_and_a_sale_of_the_same_item_wait_for_each_other(committed_engine, monkeypatch):
    """A WooCommerce stop (link state write) against a person's sale of the same item."""
    import celerp_inventory.services as svc
    from celerp.services.company_lock import lock_projections

    factory = _factory(committed_engine)
    company_id, _user_id, entity_id = await _linked_item(factory)
    entered, go = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(svc, "set_external_link", await _paused(svc.set_external_link, entered, go))

    async with factory() as store, factory() as person:
        async def stop():
            await svc.set_external_link_state(store, company_id, entity_id, _PLATFORM, sync_enabled=False)
            await store.commit()

        async def sale():
            await lock_projections(person, company_id, [entity_id])
            await person.commit()

        first = asyncio.create_task(stop())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(sale())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)


async def test_an_order_hold_and_a_first_sync_link_wait_for_each_other(committed_engine, monkeypatch):
    """An order hold (company lock, then the item's link state) against a first-sync page linking
    the same store product (upsert_external_product)."""
    import celerp.db
    import celerp_inventory.services as svc
    from celerp.services.company_lock import lock_company

    factory = _factory(committed_engine)
    company_id, _user_id, entity_id = await _linked_item(factory)
    monkeypatch.setattr(celerp.db, "SessionLocal", factory)
    entered, go = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(svc, "resolve_external_product",
                        await _paused(svc.resolve_external_product, entered, go))

    async with factory() as person:
        async def link():
            await svc.upsert_external_product(company_id, platform=_PLATFORM, product_id="77",
                                              variation_id=None, sku="HOLD-1", name="Original")

        async def hold():
            await lock_company(person, company_id)
            await svc.set_external_link_state(person, company_id, entity_id, _PLATFORM,
                                              sync_enabled=False, source="person")
            await person.commit()

        first = asyncio.create_task(link())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(hold())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)


async def test_an_attention_list_change_and_a_company_reset_wait_for_each_other(committed_engine, monkeypatch):
    """Resolving an entry on a connector's attention list (sync run row, then the company)
    against a reset of the company (company FOR UPDATE, then its sync runs)."""
    import celerp.connectors.sync_runner as runner
    from celerp.models.sync_run import SyncRun
    from celerp.services.company_lock import lock_company, lock_company_for_deletion

    factory = _factory(committed_engine)
    company_id, _user_id, _entity_id = await _seed_member_with_item(factory)
    now = datetime.now(timezone.utc)
    async with factory() as s:
        s.add(SyncRun(company_id=str(company_id), connector=_PLATFORM, entity="orders", direction="inbound",
                      started_at=now, finished_at=now, status="partial",
                      attention_json=json.dumps([{"id": "x"}])))
        await s.commit()
    entered, go = asyncio.Event(), asyncio.Event()
    real = runner._attention_run

    async def attention_run(*args, **kwargs):
        run = await real(*args, **kwargs)
        entered.set()
        await go.wait()
        return run
    monkeypatch.setattr(runner, "_attention_run", attention_run)

    async with factory() as person, factory() as resetter:
        async def resolve():
            await runner.remove_attention_entry(person, str(company_id), _PLATFORM, "orders", "x")
            await lock_company(person, company_id)
            await person.commit()

        async def reset():
            await lock_company_for_deletion(resetter, company_id)
            await resetter.execute(sa.delete(SyncRun).where(SyncRun.company_id == str(company_id)))
            await resetter.commit()

        first = asyncio.create_task(resolve())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(reset())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)


async def test_a_payment_recorded_on_an_invoice_and_a_company_reset_wait_for_each_other(committed_engine):
    """A payment from the unmatched list recorded on one of the company's invoices (the
    company, then the unmatched payment) against a reset of the company (company FOR UPDATE,
    then the payments recorded on its invoices, then its rows): the reset waits for the
    payment and puts it back on the list once the payment commits."""
    from celerp.models.payment_closure import UnmatchedPayment
    from celerp.services import payments
    from celerp.services.company_lock import lock_company_for_deletion

    factory = _factory(committed_engine)
    company_id, _user_id, _entity_id = await _seed_member_with_item(factory)
    reference = f"pi_{uuid.uuid4().hex}"
    async with factory() as s:
        s.add(UnmatchedPayment(reference=reference, amount_minor=1000, currency="USD",
                               former_company=str(uuid.uuid4()), document="doc:gone",
                               recorded_company=str(company_id), recorded_document="doc:1"))
        await s.commit()

    async with factory() as person, factory() as resetter:
        assert await payments._lock_recorded(person, reference, (company_id, "doc:1")) is not None

        async def reset():
            await lock_company_for_deletion(resetter, company_id)
            await payments.unrecord_company(resetter, company_id)
            await resetter.commit()

        second = asyncio.create_task(reset())
        await _until_blocked_or_done(committed_engine, second)
        assert not second.done(), "the reset did not wait for the payment's company lock"
        await person.commit()
        await asyncio.wait_for(second, timeout=5)
    async with factory() as s:
        kept = await s.get(UnmatchedPayment, reference)
        assert (kept.recorded_company, kept.recorded_document) == (None, None)


async def test_two_company_creates_by_one_owner_with_the_same_name_wait_for_each_other(committed_engine):
    """A company create (company row, then the owner's membership, whose foreign key shares
    the login) against a start for the same login (the login, then a company with the
    same web address). Each create holds the owner's login before its company row, so the
    second waits for the first instead of waiting on its company row in a cycle."""
    from celerp.models.company import User
    from celerp.services import provisioning
    from celerp.services.auth import hold_companyless_login

    factory = _factory(committed_engine)
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id, email=f"owner-{user_id.hex[:8]}@example.test", name="Owner",
                   auth_hash="x", is_active=True))
        await s.commit()
    name = f"Same Name {user_id.hex[:8]}"
    entered, go = asyncio.Event(), asyncio.Event()

    async with factory() as creator, factory() as starter:
        real_flush = creator.flush

        async def flush(*args, **kwargs):
            await real_flush(*args, **kwargs)
            if not entered.is_set():  # the company row is written, the membership is not
                entered.set()
                await go.wait()
        creator.flush = flush

        async def create():
            user = await creator.get(User, user_id)
            await provisioning.provision_additional_company(creator, user=user, company_name=name)
            await creator.commit()

        async def start():
            if await hold_companyless_login(starter, user_id):
                user = await starter.get(User, user_id)
                await provisioning.provision_additional_company(starter, user=user, company_name=name)
            await starter.commit()

        first = asyncio.create_task(create())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(start())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)


async def test_a_companyless_hold_and_a_session_issued_for_the_same_login_wait_for_each_other(committed_engine):
    """A session issued for a login (its session generation, then the session row, whose
    foreign key shares the login) against a start for the same login (the login, then its
    session generation as the new company's session is issued). The start holds the login
    without its key, so the session row's key share never waits on it and the start simply
    waits for the issue."""
    from celerp.services.auth import hold_companyless_login
    from celerp.services.session_tracker import lock_auth_state, register_token

    factory = _factory(committed_engine)
    company_id, user_id, _ = await _seed_member_with_item(factory)
    async with factory() as s:  # the login's session generation exists, as after any sign-in
        await lock_auth_state(s, str(user_id))
        await s.commit()
    entered, go = asyncio.Event(), asyncio.Event()

    async with factory() as issuer, factory() as starter:
        async def issue():
            await lock_auth_state(issuer, str(user_id))
            entered.set()
            await go.wait()
            await register_token(issuer, uuid.uuid4().hex, str(user_id), str(company_id),
                                 datetime.now(timezone.utc), commit=False)
            await issuer.commit()

        async def start():
            await hold_companyless_login(starter, user_id)
            await lock_auth_state(starter, str(user_id))
            await starter.commit()

        first = asyncio.create_task(issue())
        await asyncio.wait_for(entered.wait(), timeout=5)
        second = asyncio.create_task(start())
        await _until_blocked_or_done(committed_engine, second)
        go.set()
        await _both_finish(first, second)
