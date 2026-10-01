# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The private migration sink registry: only bundled first-party Celerp modules
register sinks, no HTTP route reaches a sink, and a missing module sink fails the
run before any write, naming the module to install, without enabling it."""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from migration_support import code_config, migration_env, real_engine  # noqa: F401
from migration_support import load_run, maker, staged_run

REPO = Path(__file__).resolve().parents[1]

_SINK_SOURCE = '''
class Sink:
    key = {key!r}
    groups = frozenset({groups!r})
    batch_size = 10

    async def import_batch(self, context, records):
        raise AssertionError("an untrusted sink must never receive records")

    async def reconcile(self, context, expectations):
        return []
'''


def _load_sink(folder: Path, key: str, groups: set[str]):
    """A sink class written as a module in `folder`/<package>/migration_sink.py."""
    package = folder / "untrusted_pkg"
    package.mkdir(parents=True)
    source = package / "migration_sink.py"
    source.write_text(_SINK_SOURCE.format(key=key, groups=groups))
    name = f"untrusted_sink_{abs(hash(str(source)))}"
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.Sink()


class _TestFileSink:
    key = "celerp"
    groups = frozenset({"locations"})
    batch_size = 10

    async def import_batch(self, context, records):
        raise AssertionError("a sink defined outside Celerp must never receive records")

    async def reconcile(self, context, expectations):
        return []


@pytest.fixture
def startup_sinks():
    """The registry as application startup left it, taken before any fixture replaces it."""
    from celerp.importers import sinks
    return dict(sinks._SINKS)


@pytest.mark.asyncio
async def test_first_party_sink_registration_rejects_untrusted_module(
    startup_sinks, tmp_path, monkeypatch, code_config, migration_env, real_engine,
):
    from celerp.config import read_config
    from celerp.importers import sinks
    from celerp.importers.sinks import SINK_MODULES, UntrustedSinkError, register_sink
    from celerp.main import app
    from celerp.models.company import Company
    from celerp.services import migrations

    # Registered at startup: every group is served by the bundled module that owns it.
    monkeypatch.setattr(sinks, "_SINKS", dict(startup_sinks))
    assert {group: sinks.sink_for(group).key for group in SINK_MODULES} == SINK_MODULES
    before = dict(sinks._SINKS)

    impostors = [
        # A third-party module in a folder named after a bundled module: trust is by content.
        _load_sink(tmp_path / "celerp-contacts", "celerp-contacts", {"contacts"}),
        _load_sink(tmp_path / "celerp-docs", "celerp-docs", {"documents", "settlements"}),
        # A third-party module claiming groups under its own name.
        _load_sink(tmp_path / "vendor-books", "vendor-books", {"journals"}),
        # Core-owned groups offered by code outside the core package.
        _TestFileSink(),
    ]
    for impostor in impostors:
        with pytest.raises(UntrustedSinkError, match="only bundled Celerp modules can receive a migration"):
            register_sink(impostor)
    assert sinks._SINKS == before

    # No HTTP route reaches a sink: only the migration runner resolves or calls one.
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        # The endpoint's own file: a module a test has unloaded is no longer in sys.modules.
        source = Path(inspect.getsourcefile(endpoint)).read_text()
        for name in ("sink_for(", "_SINKS", ".import_batch(", "_registered_sinks("):
            assert name not in source, (route.path, name)
    listed = subprocess.run(["git", "grep", "-l", "-e", "sink_for(", "-e", "_registered_sinks(", "--", "*.py",
                             ":!tests/"], cwd=REPO, capture_output=True, text=True, check=True)
    assert set(listed.stdout.split()) == {"celerp/importers/sinks.py", "celerp/services/migrations.py"}

    # A module sink that disappears after the start was checked fails the run before any
    # write; the owner is told plainly, never the package or the exception, and the run
    # can be discarded. A start whose module is missing is refused before anything is
    # created (test_migration_preflight).
    fake = migration_env["sink"]
    fake.groups = frozenset({"company", "locations"})
    monkeypatch.setattr(sinks, "_SINKS", {fake.key: fake})
    config_before = read_config()
    run_id, company_id, _ = await staged_run(real_engine)
    async with maker(real_engine)() as s:
        settings_before = dict((await s.get(Company, company_id)).settings or {})
    await migrations.run_migration(run_id)

    run = await load_run(real_engine, run_id)
    assert run.status == "failed"
    assert run.error_summary["message"] == migrations.FEATURE_STOPPED
    assert fake.events == []
    async with maker(real_engine)() as s:
        company = await s.get(Company, company_id)
        assert company.is_active is False
        assert dict(company.settings or {}) == settings_before
    assert read_config() == config_before
