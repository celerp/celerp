# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A migration is checked against the features this installation can run before anything
is created: a missing feature refuses the start with nothing staged, a bundled feature
that is merely off is turned on and the same run resumes after the restart, and a
failed or stranded run can always be discarded."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select  # noqa: F401

from migration_support import (  # noqa: F401
    OWNER_EMAIL,
    OWNER_PASSWORD,
    auth,
    code_config,
    count,
    fake_bytes,
    load_run,
    maker,
    migration_env,
    real_client,
    real_engine,
    save_decisions,
    scan_upload,
    staged_run,
)

pytestmark = pytest.mark.asyncio

_SETUP = {"X-Setup-Code": "abcd1234"}


@pytest.fixture
def no_journal_sink(migration_env):
    """The fake sink serves company and locations only, so journals need the bundled
    accounting module, as on a fresh install where nothing is turned on."""
    migration_env["sink"].groups = frozenset({"company", "locations"})
    return migration_env


@pytest.fixture
def module_dir(code_config, tmp_path, monkeypatch):
    """A MODULE_DIR that holds no modules, a config with nothing enabled, nothing running."""
    from celerp.config import read_config, write_config
    from celerp.modules import loader
    root = tmp_path / "modules"
    root.mkdir()
    monkeypatch.setenv("MODULE_DIR", str(root))
    monkeypatch.delenv("ENABLED_MODULES", raising=False)
    cfg = read_config()
    cfg.setdefault("modules", {})["enabled"] = []
    write_config(cfg)
    monkeypatch.setattr(loader, "_loaded", [m for m in loader._loaded if m["name"] != "celerp-accounting"])
    return root


@pytest.fixture
def restarts(monkeypatch):
    from celerp.modules import requirements
    calls: list[int] = []
    monkeypatch.setattr(requirements, "schedule_restart", lambda: calls.append(1))
    return calls


def _install_accounting(root, monkeypatch) -> None:
    """The bundled accounting module is on disk, trusted, but not turned on."""
    from celerp.modules import loader
    pkg = root / "celerp-accounting"
    pkg.mkdir()
    (pkg / "__init__.py").write_text('PLUGIN_MANIFEST = {"name": "celerp-accounting", "version": "1.0.0", '
                                     '"display_name": "Accounting"}\n')
    monkeypatch.setattr(loader, "is_first_party", lambda path: path.name.startswith("celerp-"))
    monkeypatch.setattr(loader, "first_party_names", lambda: frozenset({"celerp-accounting"}))


async def _state(engine) -> tuple[int, int, int]:
    return (await count(engine, "users"), await count(engine, "companies"), await count(engine, "migration_runs"))


async def _bootstrap_start(client, *, company_name: str = "Moved Co"):
    r = await scan_upload(client, fake_bytes(), headers=_SETUP)
    assert r.status_code == 200, r.text
    scan_token = r.json()["scan_token"]
    r = await save_decisions(client, scan_token)
    assert r.status_code == 200, r.text
    return await client.post("/migrations/bootstrap/start", headers=_SETUP, json={
        "scan_token": scan_token, "company_name": company_name, "name": "Owner",
        "email": OWNER_EMAIL, "password": OWNER_PASSWORD}), scan_token


async def test_missing_feature_refuses_start_before_anything_exists(
        real_engine, real_client, code_config, no_journal_sink, module_dir, restarts):
    from celerp.config import read_config
    before, config = await _state(real_engine), read_config()
    r, _ = await _bootstrap_start(real_client)
    assert r.status_code == 422, r.text
    detail = str(r.json()["detail"])
    assert "celerp-accounting" not in detail and "MissingSinkError" not in detail
    assert "Accounting" in detail or "accounting" in detail
    assert await _state(real_engine) == before
    assert read_config() == config and restarts == []
    assert no_journal_sink["scheduled"] == []


async def test_disabled_bundled_feature_is_prepared_and_the_same_run_resumes(
        real_engine, real_client, code_config, no_journal_sink, module_dir, restarts, monkeypatch):
    from celerp.config import read_config
    from celerp.models.company import Company
    from celerp.modules import loader
    from celerp.services import migrations
    _install_accounting(module_dir, monkeypatch)
    r, scan_token = await _bootstrap_start(real_client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["preparing"] is True
    run_id = uuid.UUID(body["run_id"])
    assert "celerp-accounting" in read_config()["modules"]["enabled"]
    assert restarts == [1] and no_journal_sink["scheduled"] == []
    run = await load_run(real_engine, run_id)
    assert run.status == "ready"
    assert run.source_summary["modules"] == ["celerp-accounting"]
    async with maker(real_engine)() as s:
        settings = (await s.get(Company, run.company_id)).settings
    assert "celerp-accounting" in settings["enabled_modules"]
    view = (await real_client.get(f"/migrations/{run_id}", headers=auth(body["access_token"]))).json()
    assert view["preparing"] is True

    # After the restart the module is running and its sink serves journals: startup
    # resumes the same run, exactly once.
    monkeypatch.setattr(loader, "_loaded", [*loader._loaded, {"name": "celerp-accounting", "version": "1.0.0"}])
    no_journal_sink["sink"].groups = frozenset({"company", "locations", "journals"})
    async with maker(real_engine)() as s:
        assert await migrations.resume_prepared_runs(s) == 1
    async with maker(real_engine)() as s:
        assert await migrations.resume_prepared_runs(s) == 0
    assert no_journal_sink["scheduled"] == [run_id]
    assert (await load_run(real_engine, run_id)).status == "running"
    assert await _state(real_engine) == (1, 1, 1)


async def test_prepared_run_waits_while_feature_still_not_running(
        real_engine, real_client, code_config, no_journal_sink, module_dir, restarts, monkeypatch):
    """A restart that did not bring the feature up leaves the run ready, never failed."""
    from celerp.services import migrations
    _install_accounting(module_dir, monkeypatch)
    r, _ = await _bootstrap_start(real_client)
    run_id = uuid.UUID(r.json()["run_id"])
    async with maker(real_engine)() as s:
        assert await migrations.resume_prepared_runs(s) == 0
    assert (await load_run(real_engine, run_id)).status == "ready"
    assert no_journal_sink["scheduled"] == []


async def test_config_failure_leaves_nothing_staged(
        real_engine, real_client, code_config, no_journal_sink, module_dir, restarts, monkeypatch):
    from celerp.modules import requirements
    _install_accounting(module_dir, monkeypatch)

    def broken(names):
        raise OSError("read-only file system")
    monkeypatch.setattr(requirements, "set_enabled_modules", broken)
    before = await _state(real_engine)
    r, _ = await _bootstrap_start(real_client)
    assert r.status_code == 503, r.text
    assert "read-only" not in r.text
    assert await _state(real_engine) == before and restarts == []


async def test_repeated_start_while_preparing_reuses_one_run(
        real_engine, real_client, code_config, no_journal_sink, module_dir, restarts, monkeypatch):
    """A double-clicked or retried owner start after preparation reuses the same run."""
    _install_accounting(module_dir, monkeypatch)
    r, scan_token = await _bootstrap_start(real_client)
    assert r.status_code == 201
    tok = r.json()["access_token"]
    r2 = await real_client.post("/migrations/start-from-scan", headers=auth(tok),
                                json={"scan_token": scan_token, "company_name": "Moved Co"})
    assert r2.status_code in (200, 404, 409), r2.text
    assert await _state(real_engine) == (1, 1, 1)


async def test_failed_run_view_has_no_internals(real_engine, code_config, migration_env, monkeypatch):
    """A run that failed before any write shows a plain message and never says it is still
    running; exception classes, cursors and package names stay out of the view."""
    from celerp.importers import sinks
    from celerp.services import migrations
    fake = migration_env["sink"]
    fake.groups = frozenset({"company", "locations"})
    monkeypatch.setattr(sinks, "_SINKS", {fake.key: fake})
    run_id, _, user_id = await staged_run(real_engine)
    await migrations.run_migration(run_id)
    async with maker(real_engine)() as s:
        run = await migrations.get_owned_migration_run(s, run_id, user_id)
        view = await migrations.run_view(s, run)
    text = repr(view)
    for raw in ("MissingSinkError", "error_class", "batch_cursor", "celerp-accounting"):
        assert raw not in text, raw
    assert view["status"] == "failed" and view["error"]["message"]


async def test_discard_after_sink_loss_succeeds_and_skips_absent_tables(
        real_engine, code_config, migration_env, monkeypatch):
    """A run that failed because a feature disappeared after preflight is discarded
    cleanly, even when a table the discard order names does not exist here."""
    from celerp.importers import sinks
    from celerp.models.company import Company
    from celerp.services import migrations
    fake = migration_env["sink"]
    fake.groups = frozenset({"company", "locations"})
    monkeypatch.setattr(sinks, "_SINKS", {fake.key: fake})
    monkeypatch.setattr(migrations, "_DISCARD_ORDER", ("zz_absent_table", *migrations._DISCARD_ORDER))
    run_id, company_id, user_id = await staged_run(real_engine)
    await migrations.run_migration(run_id)
    assert (await load_run(real_engine, run_id)).status == "failed"
    assert fake.events == []
    async with maker(real_engine)() as s:
        run = await migrations.get_owned_migration_run(s, run_id, user_id)
        await migrations.discard(s, run)
    async with maker(real_engine)() as s:
        assert await s.get(Company, company_id) is None
        assert await s.scalar(select(func.count()).select_from(
            select(1).where(Company.id == company_id).subquery())) == 0
