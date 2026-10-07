# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""POST /accounting/accounts/import/batch: add accounts from a chart file.

The import only ever adds. An account code already in the chart is skipped and
listed, never changed, because retyping or deactivating an account that postings
use would break them; there is no upsert or replace mode. Every row is checked
for code, name and type by the same rules as creating one account, and parent
references resolve against the chart as it will be after the import, whatever
order the rows arrive in. A parent that is missing, or a chain of parents that
loops back on itself, makes the row invalid and it is reported, not written.

Registration seeds the default chart, so these tests use codes in the 8000s,
which it does not contain.
"""

from __future__ import annotations

import uuid

import pytest

from test_helpers import grant_permission, perm_setup

PATH = "/accounting/accounts/import/batch"
PREVIEW_PATH = "/accounting/accounts/import/preview"


async def _reg(client) -> dict:
    addr = f"chart-{uuid.uuid4().hex[:8]}@import.test"
    r = await client.post("/auth/register", json={
        "company_name": "ChartCo", "email": addr, "name": "Admin", "password": "validpass1"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _row(code, name="Account", account_type="asset", parent_code=None, **extra) -> dict:
    return {"code": code, "name": name, "account_type": account_type, "parent_code": parent_code, **extra}


async def _chart(client, h) -> dict[str, dict]:
    r = await client.get("/accounting/chart", headers=h)
    assert r.status_code == 200, r.text
    return {a["code"]: a for a in r.json()["items"]}


async def _legacy_account(client, session, h, code, parent_code) -> None:
    """A row from before parents were checked, which the chart can still hold."""
    from celerp_accounting.models import Account

    r = await client.get("/companies/me", headers=h)
    assert r.status_code == 200, r.text
    session.add(Account(id=uuid.uuid4(), company_id=uuid.UUID(r.json()["id"]), code=code,
                        name=f"Legacy {code}", account_type="asset", parent_code=parent_code))
    await session.commit()


async def _import(client, h, records, **body):
    return await client.post(PATH, headers=h, json={"records": records, **body})


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_requires_manage_accounting(client, session):
    s = await perm_setup(client, session)
    await grant_permission(client, s["admin_h"], "manage_accounting", "admin")
    r = await _import(client, s["manager_h"], [_row("8100", "Denied")])
    assert r.status_code == 403, r.text
    assert "8100" not in await _chart(client, s["admin_h"])


@pytest.mark.asyncio
async def test_chart_import_requires_import_export_data(client, session):
    s = await perm_setup(client, session)
    await grant_permission(client, s["admin_h"], "import_export_data", "admin")
    r = await _import(client, s["manager_h"], [_row("8100", "Denied")])
    assert r.status_code == 403, r.text
    assert "8100" not in await _chart(client, s["admin_h"])


@pytest.mark.asyncio
async def test_chart_import_refuses_operator(client, session):
    s = await perm_setup(client, session)
    r = await _import(client, s["operator_h"], [_row("8100", "Denied")])
    assert r.status_code == 403, r.text
    r = await _import(client, s["manager_h"], [_row("8101", "Allowed")])
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1


# ---------------------------------------------------------------------------
# Add-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_adds_new_accounts(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8000", "Holding", "equity"),
        _row("8010", "Holding Reserve", "equity", "8000", is_active="false"),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["errors"]) == (2, 0, [])
    chart = await _chart(client, h)
    assert chart["8000"]["name"] == "Holding" and chart["8000"]["account_type"] == "equity"
    assert chart["8000"]["parent_code"] is None and chart["8000"]["is_active"] is True
    assert chart["8010"]["parent_code"] == "8000" and chart["8010"]["is_active"] is False


@pytest.mark.asyncio
async def test_chart_import_existing_code_is_skipped_reported_and_unchanged(client):
    h = await _reg(client)
    before = (await _chart(client, h))["1110"]
    r = await _import(client, h, [
        _row("1110", "Renamed Cash", "expense", "6000", is_active="false"),
        _row("8200", "New One"),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["errors"]) == (1, 1, [])
    assert body["skipped_codes"] == ["1110"]
    assert (await _chart(client, h))["1110"] == before


@pytest.mark.asyncio
async def test_chart_import_existing_code_is_listed_once_whatever_the_row_says(client):
    h = await _reg(client)
    before = (await _chart(client, h))["1110"]
    r = await _import(client, h, [
        _row(" 1110 ", "Renamed Cash"),
        _row("1110", "Bad Type", "not-a-type"),
        _row("1110", ""),
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["created"], body["skipped"], body["errors"], body["skipped_codes"]) == (0, 1, [], ["1110"])
    assert (await _chart(client, h))["1110"] == before


@pytest.mark.asyncio
async def test_chart_import_rejects_upsert_mode(client):
    h = await _reg(client)
    before = (await _chart(client, h))["1110"]
    r = await _import(client, h, [_row("1110", "Renamed Cash"), _row("8300", "New")], upsert=True)
    assert r.status_code == 422, r.text
    assert "only adds" in r.text
    chart = await _chart(client, h)
    assert chart["1110"] == before and "8300" not in chart


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"mode": "replace"}, {"mode": "upsert"}, {"replace": True}])
async def test_chart_import_rejects_replace_mode(client, extra):
    h = await _reg(client)
    r = await _import(client, h, [_row("8300", "New")], **extra)
    assert r.status_code == 422, r.text
    assert "8300" not in await _chart(client, h)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["upsert=true", "mode=replace"])
async def test_chart_import_rejects_options_in_the_query_string(client, query):
    h = await _reg(client)
    r = await client.post(f"{PATH}?{query}", headers=h, json={"records": [_row("8300", "New")]})
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "The chart import takes no query parameters."
    assert "8300" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_chart_import_repeat_same_file_creates_nothing(client):
    h = await _reg(client)
    rows = [_row("8400", "Parent"), _row("8410", "Child", parent_code="8400")]
    first = (await _import(client, h, rows)).json()
    chart = await _chart(client, h)
    second = await _import(client, h, rows)
    assert second.status_code == 200, second.text
    assert first["created"] == 2
    assert (second.json()["created"], second.json()["skipped"]) == (0, 2)
    assert second.json()["skipped_codes"] == ["8400", "8410"]
    assert await _chart(client, h) == chart


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("row, message", [
    (_row(""), "code is required"),
    (_row("   "), "code is required"),
    (_row("8" * 33), "32 characters"),
    (_row("8500", ""), "name is required"),
    (_row("8500", "  "), "name is required"),
    (_row("8500", "Bad Type", "Current Asset"), "Account type must be one of"),
    (_row("8500", "No Type", ""), "Account type must be one of"),
    ({"code": 8500, "name": "Numeric", "account_type": "asset"}, "code must be text"),
    ("not a row", "must be an object"),
    (_row("8500", "Bad\x00Name"), "Account name cannot contain a NUL character."),
    (_row("85\x0000"), "Account code cannot contain a NUL character."),
    (_row("8500", parent_code="85\x0001"), "Parent code cannot contain a NUL character."),
])
async def test_chart_import_validates_code_name_type(client, row, message):
    h = await _reg(client)
    r = await _import(client, h, [row, _row("8501", "Good")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 1 and message in body["errors"][0], body["errors"]
    chart = await _chart(client, h)
    assert "8501" in chart and "8500" not in chart


@pytest.mark.asyncio
@pytest.mark.parametrize("payload, message", [
    ({"code": "", "name": "Blank Code", "account_type": "asset"}, "code is required"),
    ({"code": "8" * 33, "name": "Long Code", "account_type": "asset"}, "32 characters"),
    ({"code": "8600", "name": " ", "account_type": "asset"}, "name is required"),
])
async def test_create_account_applies_the_same_code_and_name_rules(client, payload, message):
    h = await _reg(client)
    r = await client.post("/accounting/accounts", headers=h, json=payload)
    assert r.status_code == 422, r.text
    assert message in r.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_account_parent_code_is_trimmed_and_length_checked(client):
    h = await _reg(client)
    r = await client.post("/accounting/accounts", headers=h, json={
        "code": "8610", "name": "Long Parent", "account_type": "asset", "parent_code": "8" * 40})
    assert r.status_code == 422, r.text
    assert "Parent code must be 32 characters" in r.json()["detail"]["message"]
    r = await client.post("/accounting/accounts", headers=h, json={
        "code": "8611", "name": "Blank Parent", "account_type": "asset", "parent_code": "  "})
    assert r.status_code == 200, r.text
    assert r.json()["parent_code"] is None
    r = await client.patch("/accounting/accounts/8611", headers=h, json={"parent_code": "8" * 40})
    assert r.status_code == 422, r.text
    assert "Parent code must be 32 characters" in r.json()["detail"]["message"]
    r = await client.patch("/accounting/accounts/8611", headers=h, json={"parent_code": " 1000 "})
    assert r.status_code == 200, r.text
    assert (await _chart(client, h))["8611"]["parent_code"] == "1000"


@pytest.mark.asyncio
async def test_bank_account_code_follows_the_account_code_rules(client):
    """A bank account adds a chart account, so its code is checked the same way and a
    chart import of the same code later finds it instead of adding a near copy."""
    h = await _reg(client)
    bank = {"bank_name": "Code Bank", "account_number": "4321", "bank_type": "checking",
            "currency": "USD", "opening_balance": 0}
    r = await client.post("/accounting/bank-accounts", headers=h, json={**bank, "account_code": "Q" * 33})
    assert r.status_code == 422, r.text
    assert "32 characters" in r.json()["detail"]["message"]
    r = await client.post("/accounting/bank-accounts", headers=h, json={**bank, "account_code": " 8620 "})
    assert r.status_code == 200, r.text
    assert "8620" in await _chart(client, h)
    r = await _import(client, h, [_row("8620", "Bank Again")])
    assert (r.json()["created"], r.json()["skipped_codes"]) == (0, ["8620"])


# ---------------------------------------------------------------------------
# File size
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_takes_a_file_of_more_than_500_accounts(client):
    h = await _reg(client)
    rows = [_row(f"A{i:04d}", f"Account {i}") for i in range(1200)]
    r = await _import(client, h, rows)
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["errors"]) == (1200, [])


@pytest.mark.asyncio
async def test_chart_import_over_the_limit_says_so_plainly(client):
    h = await _reg(client)
    rows = [_row(f"A{i:04d}", f"Account {i}") for i in range(2001)]
    r = await _import(client, h, rows)
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "A chart file can hold up to 2000 accounts; this one has 2001."
    assert "A0000" not in await _chart(client, h)


# ---------------------------------------------------------------------------
# Parents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_resolves_parent_later_in_file(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8720", "Grandchild", parent_code="8710"),
        _row("8710", "Child", parent_code="8700"),
        _row("8700", "Root"),
    ])
    assert r.status_code == 200, r.text
    assert (r.json()["created"], r.json()["errors"]) == (3, [])
    chart = await _chart(client, h)
    assert chart["8720"]["parent_code"] == "8710" and chart["8710"]["parent_code"] == "8700"


@pytest.mark.asyncio
async def test_chart_import_resolves_parent_already_in_chart(client):
    h = await _reg(client)
    r = await _import(client, h, [_row("1160", "Deposits", parent_code="1100")])
    assert (r.json()["created"], r.json()["errors"]) == (1, [])
    assert (await _chart(client, h))["1160"]["parent_code"] == "1100"


@pytest.mark.asyncio
async def test_chart_import_missing_parent_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [_row("8800", "Orphan", parent_code="9999"), _row("8801", "Fine")])
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 1
    assert "8800" in body["errors"][0] and "9999" in body["errors"][0]
    assert "8800" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_chart_import_cycle_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8900", "A", parent_code="8920"),
        _row("8910", "B", parent_code="8900"),
        _row("8920", "C", parent_code="8910"),
        _row("8930", "Self", parent_code="8930"),
        _row("8940", "Fine"),
    ])
    body = r.json()
    assert body["created"] == 1
    assert len(body["errors"]) == 4
    assert all("loop" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert not {"8900", "8910", "8920", "8930"} & set(chart) and "8940" in chart


@pytest.mark.asyncio
async def test_chart_import_cycle_through_existing_account_is_invalid(client, session):
    h = await _reg(client)
    # An account already in the chart whose parent code names nothing yet.
    await _legacy_account(client, session, h, "8950", "8960")
    r = await _import(client, h, [_row("8960", "Closes The Loop", parent_code="8950")])
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 1 and "loop" in body["errors"][0]
    assert "8960" not in await _chart(client, h)


@pytest.mark.asyncio
async def test_chart_import_row_under_a_loop_already_in_the_chart_is_invalid(client, session):
    h = await _reg(client)
    # An account already in the chart that is its own parent.
    await _legacy_account(client, session, h, "8955", "8955")
    r = await _import(client, h, [
        _row("8956", "Under The Loop", parent_code="8955"),
        _row("8957", "Under That", parent_code="8956"),
    ])
    body = r.json()
    assert body["created"] == 0
    assert any("8956" in e and "loop (8956 > 8955 > 8955)" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert "8956" not in chart and "8957" not in chart


@pytest.mark.asyncio
async def test_chart_import_child_of_invalid_row_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8971", "Child", parent_code="8970"),
        _row("8970", "Bad Parent", "not-a-type"),
    ])
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 2
    assert any("8971" in e and "8970" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert "8970" not in chart and "8971" not in chart


@pytest.mark.asyncio
async def test_chart_import_duplicate_code_in_file_is_invalid(client):
    h = await _reg(client)
    r = await _import(client, h, [
        _row("8980", "First"), _row("8980", "Second"), _row("8981", "Child", parent_code="8980"),
    ])
    body = r.json()
    assert body["created"] == 0
    assert any("more than once" in e for e in body["errors"]), body["errors"]
    chart = await _chart(client, h)
    assert "8980" not in chart and "8981" not in chart


# ---------------------------------------------------------------------------
# is_active
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_parses_is_active_explicitly(client):
    h = await _reg(client)
    cases = {
        "false": False, "FALSE": False, " no ": False, "0": False, False: False,
        "true": True, "Yes": True, "1": True, True: True, "": True, None: True,
    }
    rows = [_row(f"86{i:02d}", f"Case {i}", is_active=v) for i, v in enumerate(cases)]
    rows.append(_row("8699", "Missing field"))
    r = await _import(client, h, rows)
    assert (r.json()["created"], r.json()["errors"]) == (len(rows), [])
    chart = await _chart(client, h)
    for i, expected in enumerate(cases.values()):
        assert chart[f"86{i:02d}"]["is_active"] is expected, (i, expected)
    assert chart["8699"]["is_active"] is True


@pytest.mark.asyncio
async def test_chart_import_is_active_takes_the_numbers_1_and_0(client):
    h = await _reg(client)
    r = await _import(client, h, [_row("8680", "One", is_active=1), _row("8681", "Zero", is_active=0)])
    assert (r.json()["created"], r.json()["errors"]) == (2, [])
    chart = await _chart(client, h)
    assert chart["8680"]["is_active"] is True and chart["8681"]["is_active"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["maybe", "inactive", "2", 2, 1.5, [True]])
async def test_chart_import_rejects_unrecognized_is_active(client, value):
    h = await _reg(client)
    r = await _import(client, h, [_row("8690", "Odd", is_active=value)])
    body = r.json()
    assert body["created"] == 0
    assert len(body["errors"]) == 1 and "is_active" in body["errors"][0]
    assert "8690" not in await _chart(client, h)


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chart_import_preview_reports_what_the_import_would_do_and_writes_nothing(client):
    h = await _reg(client)
    rows = [
        _row("1110", "Renamed Cash"),
        _row("8990", "New Parent"),
        _row("8991", "New Child", parent_code="8990"),
        _row("8992", "Orphan", parent_code="8999"),
    ]
    before = await _chart(client, h)
    preview = await client.post(PREVIEW_PATH, headers=h, json={"records": rows})
    assert preview.status_code == 200, preview.text
    assert await _chart(client, h) == before
    imported = await _import(client, h, rows)
    assert preview.json() == imported.json()
    assert (preview.json()["created"], preview.json()["skipped_codes"]) == (2, ["1110"])
    assert "8999" in preview.json()["errors"][0]


@pytest.mark.asyncio
async def test_chart_import_preview_needs_the_same_permissions(client, session):
    s = await perm_setup(client, session)
    r = await client.post(PREVIEW_PATH, headers=s["operator_h"], json={"records": [_row("8100", "Denied")]})
    assert r.status_code == 403, r.text
    await grant_permission(client, s["admin_h"], "import_export_data", "admin")
    r = await client.post(PREVIEW_PATH, headers=s["manager_h"], json={"records": [_row("8100", "Denied")]})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Authority is judged again once the import holds the company lock
# ---------------------------------------------------------------------------


async def _seed_manager(factory) -> tuple[uuid.UUID, uuid.UUID]:
    from celerp.models.accounting import UserCompany
    from celerp.models.company import Company, User

    company_id, user_id = uuid.uuid4(), uuid.uuid4()
    async with factory() as s:
        s.add(Company(id=company_id, name="ChartRace", slug=f"chart-{company_id.hex[:8]}", settings={}))
        s.add(User(id=user_id, email=f"m-{user_id.hex[:8]}@example.test", name="Manager",
                   auth_hash="x", is_active=True))
        await s.flush()
        s.add(UserCompany(user_id=user_id, company_id=company_id, role="manager", is_active=True))
        await s.commit()
    return company_id, user_id


async def _revoke_role(s, company_id, user_id):
    from sqlalchemy import update
    from celerp.models.accounting import UserCompany
    await s.execute(update(UserCompany).where(
        UserCompany.user_id == user_id, UserCompany.company_id == company_id,
    ).values(role="viewer"))


def _revoke_grant(key):
    async def _revoke(s, company_id, user_id):
        from celerp.services.company_lock import locked_company
        company = await locked_company(s, company_id)
        company.settings = {**(company.settings or {}), "role_grants": {key: ["owner"]}}
    return _revoke


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoke", [_revoke_role, _revoke_grant("manage_accounting"), _revoke_grant("import_export_data")],
    ids=["role", "manage_accounting", "import_export_data"],
)
async def test_chart_import_refuses_authority_revoked_while_it_waited_for_the_lock(
    committed_engine, monkeypatch, revoke,
):
    """The request was authorized, then waited for the company lock while another
    transaction took the caller's role or a required permission away. Once the
    import holds the lock it is judged by that committed change: refused, and no
    account is added."""
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from starlette.requests import Request

    import celerp.services.company_lock as company_lock
    from celerp_accounting import routes
    from celerp_accounting.models import Account

    factory = async_sessionmaker(bind=committed_engine, class_=AsyncSession, expire_on_commit=False)
    company_id, user_id = await _seed_manager(factory)

    paused, release = asyncio.Event(), asyncio.Event()
    real_lock = company_lock.lock_company

    async def _held(session, cid):
        if not paused.is_set():
            paused.set()
            await release.wait()
        return await real_lock(session, cid)

    monkeypatch.setattr(company_lock, "lock_company", _held)
    monkeypatch.setattr(routes, "lock_company", _held, raising=False)

    request = Request({"type": "http", "query_string": b"", "headers": []})
    body = routes.ChartImportRequest(records=[_row("8800", "Revoked import")])
    async with factory() as s:
        task = asyncio.create_task(routes.import_chart_accounts(
            request, body, company_id=company_id, user=SimpleNamespace(id=user_id), session=s,
        ))
        await asyncio.wait_for(paused.wait(), timeout=10)
        async with factory() as other:
            await revoke(other, company_id, user_id)
            await other.commit()
        release.set()
        with pytest.raises(HTTPException) as err:
            await asyncio.wait_for(task, timeout=30)
    assert err.value.status_code == 403

    async with factory() as s:
        codes = (await s.execute(select(Account.code).where(Account.company_id == company_id))).scalars().all()
    assert "8800" not in codes
