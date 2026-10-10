# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""While a System Recovery is unfinished only the anonymous liveness probes
answer. Every other route, including both readiness probes, the authenticated
host health page and the other internal probes, is refused before it runs: the
installation does not report itself ready, and no session from before the
recovery is checked against a half-restored database."""

from __future__ import annotations

import pytest

from celerp.services import backup_import
from ui.i18n import t

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


@pytest.mark.parametrize("path", ["/health/ready", "/__celerp/ready", "/health/system", "/companies/me"])
async def test_readiness_and_other_routes_are_refused_before_they_run(client, unfinished_recovery, path):
    """Readiness stays 503 until recovery converges, and neither the session nor
    the database is touched to say so."""
    from celerp.db import get_session
    from celerp.main import app
    from celerp.services.auth import get_current_user

    calls = []

    async def _user():
        calls.append("auth")
        return None

    async def _session():
        calls.append("db")
        yield None

    saved = dict(app.dependency_overrides)
    app.dependency_overrides.update({get_current_user: _user, get_session: _session})
    try:
        r = await client.get(path, headers={"Authorization": "Bearer old-session"})
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)
    assert r.status_code == 503, (path, r.text)
    assert r.json()["detail"] == t("error.recovery_incomplete", "en")
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
    for path in ("/health/ready", "/__celerp/ready"):
        r = await client.get(path)
        assert r.status_code == 200 and r.json() == {"status": "ok", "db": "ok"}, (path, r.text)
    assert (await client.get("/__celerp/drain")).status_code == 200
    assert (await client.get("/health/system")).status_code == 401
