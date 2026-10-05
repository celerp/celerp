# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Factory reset on a real request session: signing in has already read the user and the
company on that session, and the reset must still wipe the company and answer ok."""

from __future__ import annotations

import pytest

from migration_support import OWNER_EMAIL, OWNER_PASSWORD, auth, count, real_client, real_engine  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio


async def test_factory_reset_wipes_the_company_on_a_real_session(real_client, real_engine):  # noqa: F811
    r = await real_client.post("/auth/register", json={
        "company_name": "Reset Co", "email": OWNER_EMAIL, "name": "Owner", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    assert await count(real_engine, "companies", "name = :n", n="Reset Co") == 1

    r = await real_client.post("/system/factory-reset", headers=auth(token))

    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    assert await count(real_engine, "companies", "name = :n", n="Reset Co") == 0
    assert await count(real_engine, "users") == 0
