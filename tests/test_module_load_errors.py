# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Loader error surfacing: a module that fails to load must leave a readable
reason in load_errors() instead of disappearing silently at boot."""
from __future__ import annotations

from pathlib import Path

from celerp.modules.loader import load_all, load_errors, loaded_modules

OK_MODULE = '''PLUGIN_MANIFEST = {
    "name": "ok-module",
    "version": "1.0.0",
    "display_name": "OK Module",
}
'''

BROKEN_MODULE = '''raise RuntimeError("boom at import time")
'''

NO_MANIFEST = '''x = 1
'''

NEEDS_MISSING_DEP = '''PLUGIN_MANIFEST = {
    "name": "needs-dep",
    "version": "1.0.0",
    "depends_on": ["not-installed"],
}
'''


def _mk(dirpath: Path, name: str, init_src: str) -> None:
    pkg = dirpath / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_src)


def test_load_errors_are_recorded(tmp_path):
    _mk(tmp_path, "ok-module", OK_MODULE)
    _mk(tmp_path, "broken-module", BROKEN_MODULE)
    _mk(tmp_path, "no-manifest", NO_MANIFEST)
    _mk(tmp_path, "needs-dep", NEEDS_MISSING_DEP)

    loaded = load_all(tmp_path, {"ok-module", "broken-module", "no-manifest", "needs-dep"})

    names = {m["name"] for m in loaded}
    assert "ok-module" in names

    errs = load_errors()
    assert "broken-module" in errs and "boom at import time" in errs["broken-module"]
    assert "no-manifest" in errs and "PLUGIN_MANIFEST" in errs["no-manifest"]
    assert "needs-dep" in errs and "not-installed" in errs["needs-dep"]
    # the good module carries no error
    assert "ok-module" not in errs


def test_errors_cleared_between_runs(tmp_path):
    _mk(tmp_path, "broken-module", BROKEN_MODULE)
    load_all(tmp_path, {"broken-module"})
    assert "broken-module" in load_errors()

    ok_dir = tmp_path / "second"
    ok_dir.mkdir()
    _mk(ok_dir, "ok-module", OK_MODULE)
    load_all(ok_dir, {"ok-module"})
    assert load_errors() == {}
    assert any(m["name"] == "ok-module" for m in loaded_modules())
