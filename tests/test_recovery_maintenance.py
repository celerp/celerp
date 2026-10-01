# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""While a System Recovery is unfinished only the anonymous liveness and
readiness probes answer. Every other route, including the authenticated host
health page and the other internal probes, is refused before it runs, so no
session from before the recovery is checked against a half-restored database."""

from __future__ import annotations

import pytest

from celerp.services import backup_import

pytestmark = pytest.mark.asyncio


@pytest.fixture
def unfinished_recovery(tmp_path, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    (tmp_path / backup_import.RECOVERY_MARKER).write_text("{}")
    assert backup_import.recovery_incomplete()


@pytest.mark.parametrize("path", ["/health", "/__celerp/health"])
async def test_liveness_probe_answers_during_unfinished_recovery(client, unfinished_recovery, path):
    r = await client.get(path)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"


@pytest.mark.parametrize("path", ["/health/ready", "/__celerp/ready"])
async def test_readiness_probe_answers_during_unfinished_recovery(client, unfinished_recovery, path):
    r = await client.get(path)
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "db": "ok"}


async def test_host_health_is_refused_without_checking_the_session(client, unfinished_recovery):
    from celerp.main import app
    from celerp.services.auth import get_current_user

    calls = []

    async def _user():
        calls.append(True)
        return None

    app.dependency_overrides[get_current_user] = _user
    try:
        r = await client.get("/health/system", headers={"Authorization": "Bearer old-session"})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 503, r.text
    assert "System Recovery did not finish" in r.json()["detail"]
    assert calls == []


@pytest.mark.parametrize("path", [
    "/__celerp/drain", "/__celerp/", "/__celerp/anything", "/health/system", "/health/other",
    "/health/ready/", "/healthz", "/health/../companies/me",
])
async def test_other_probe_paths_are_refused_during_unfinished_recovery(client, unfinished_recovery, path):
    r = await client.get(path)
    assert r.status_code == 503, (path, r.text)


async def test_probes_and_routes_answer_normally_without_a_recovery_marker(client, tmp_path, monkeypatch):
    from celerp.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    assert (await client.get("/__celerp/drain")).status_code == 200
    assert (await client.get("/health/system")).status_code == 401
