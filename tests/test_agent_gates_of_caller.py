# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The gate markers the tool compiler reads, and which gates a signed-in caller passes."""
from __future__ import annotations

import os
import uuid

import pytest

from celerp.agent_markers import agent_gate
from celerp.ai.tools import gates_of_caller
from celerp.services.auth import is_install_owner


def test_agent_gate_tags_and_returns_the_dependency():
    def dep():
        return None

    assert agent_gate(dep, "owner") is dep
    assert dep.agent_gate == "owner"


def test_agent_gate_refuses_an_unknown_rule():
    with pytest.raises(ValueError):
        agent_gate(lambda: None, "admin")


@pytest.mark.asyncio
async def test_gates_of_caller_follow_role_and_installation(client, session):
    r = await client.post("/auth/register", json={
        "company_name": "Gates Co", "email": f"own-{os.urandom(4).hex()}@example.com",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    r = await client.post("/companies/me/users", headers=h, json={
        "name": "Viewer", "email": f"v-{os.urandom(4).hex()}@example.com",
        "password": "testpass123", "role": "viewer"})
    assert r.status_code == 200, r.text
    members = (await client.get("/companies/me/users", headers=h)).json()["items"]
    ids = {m["role"]: uuid.UUID(m["id"]) for m in members}

    root = await is_install_owner(session, ids["owner"])
    assert await gates_of_caller(session, ids["owner"], "owner") == (
        frozenset({"owner", "install_owner"}) if root else frozenset({"owner"}))
    assert await gates_of_caller(session, ids["viewer"], "viewer") == frozenset()
    # The installation gate follows the login, not the role it holds here.
    assert await gates_of_caller(session, ids["owner"], "viewer") == (
        frozenset({"install_owner"}) if root else frozenset())
