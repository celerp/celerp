# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A lookup index never stops Celerp starting. Every revision whose upgrade builds an index
concurrently (found by reading the revisions, so the next one is covered) is run against a
stand-in ``op`` that fails at each point of the build in turn: the drop of an invalid leftover,
each create, the validity read, an index left invalid, and each of those again with the
clean-up drop failing too. In every case the upgrade returns, a warning names each index that
failed, the drop of what its build left was tried, and every other index of the revision is
built and kept. The app is correct without the index: lookups read through the old plan until
the next start builds it again."""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import logging
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parents[2] / "celerp" / "migrations" / "versions"


def _concurrent_index_revisions() -> dict[Path, list[str]]:
    """Each revision file whose upgrade() creates an index concurrently, with the names it builds."""
    found = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in tree.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "upgrade"):
                continue
            names = [c.args[0].value for c in ast.walk(fn) if isinstance(c, ast.Call)
                     and getattr(c.func, "attr", "") == "create_index"
                     and any(k.arg == "postgresql_concurrently" for k in c.keywords)
                     and c.args and isinstance(c.args[0], ast.Constant)]
            if names:
                found[path] = names
    return found


REVISIONS = _concurrent_index_revisions()


class _Failure(Exception):
    pass


class _Op:
    """The parts of alembic's ``op`` an index build uses, over a table of index states
    (absent None, valid True, invalid False), failing where told."""

    def __init__(self, names, *, leftover=False, fail_drop=False, fail_create=None, fail_read=False,
                 stays_invalid=False, fail_tail=False):
        self.state = {n: (False if leftover else None) for n in names}
        self.fail_drop, self.fail_create, self.fail_read = fail_drop, fail_create, fail_read
        self.stays_invalid, self.fail_tail = stays_invalid, fail_tail
        self.failed = False
        self.created: set[str] = set()
        self.drops_after_failure: list[str] = []

    def _fail(self):
        self.failed = True
        raise _Failure("injected")

    @contextlib.contextmanager
    def _autocommit(self):
        yield

    def get_context(self):
        return type("Ctx", (), {"autocommit_block": lambda _: self._autocommit()})()

    def get_bind(self):
        op = self

        class _Result:
            def __init__(self, value):
                self.value = value

            def scalar(self):
                return self.value

        class _Bind:
            def execute(self, statement, params=None):
                if "indisvalid" in str(statement):
                    if op.fail_read:
                        op._fail()
                    name = (params or {}).get("name")
                    if name in op.created and op.state.get(name) is False:
                        op.failed = True  # the build finished but left the index invalid
                    return _Result(op.state.get(name))
                if "to_regclass" in str(statement):
                    return _Result(params["name"] if op.state.get(params["name"]) is not None else None)
                return _Result(None)

        return _Bind()

    def create_index(self, name, *args, **kwargs):
        if name == self.fail_create:
            self._fail()
        self.created.add(name)
        self.state[name] = False if self.stays_invalid else True

    def drop_index(self, name, *args, if_exists=False, **kwargs):
        # The clean-up drop of what a failed build left is the one that tolerates no index.
        if if_exists:
            self.drops_after_failure.append(name)
            if self.fail_tail:
                raise _Failure("injected tail")
        elif self.fail_drop:
            self._fail()
        self.state[name] = None


def _cases():
    for path, names in REVISIONS.items():
        points = [("leftover drop", {"leftover": True, "fail_drop": True}),
                  ("validity read", {"fail_read": True}),
                  ("left invalid", {"stays_invalid": True})]
        points += [(f"create {n}", {"fail_create": n}) for n in names]
        for label, kw in points:
            for tail in (False, True):
                yield pytest.param(path, names, kw, tail,
                                   id=f"{path.stem.split('_')[0]}-{label}{'-tail fails' if tail else ''}")


def _load(path: Path, op: _Op, monkeypatch):
    monkeypatch.setattr("celerp.migrations._lookup_index.op", op)
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = op
    # A data step before the index build is out of scope here: it reads real rows.
    for name in ("update_projection_state",):
        if hasattr(module, name):
            setattr(module, name, lambda *a, **k: None)
    return module


def test_every_concurrent_index_revision_is_found():
    assert len(REVISIONS) >= 3, REVISIONS


@pytest.mark.parametrize(("path", "names", "failure", "tail"), list(_cases()))
def test_a_failed_lookup_index_build_never_stops_the_upgrade(path, names, failure, tail, caplog, monkeypatch):
    op = _Op(names, fail_tail=tail, **failure)
    module = _load(path, op, monkeypatch)
    with caplog.at_level(logging.WARNING):
        module.upgrade()
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    failed = {failure["fail_create"]} if "fail_create" in failure else set(names)
    assert all(n in warned for n in failed), (path.name, warned)
    # Each failed index is dropped if present, and nothing else: the other indexes of the revision
    # stay built, and an index the revision would have replaced on success stays, so lookups keep it.
    assert set(op.drops_after_failure) == failed, (path.name, op.drops_after_failure)
    assert {n for n in names if op.state[n] is True} == set(names) - failed, (path.name, op.state)


@pytest.mark.parametrize(("failure", "answer"), [({}, True), ({"stays_invalid": True}, False),
                                                 ({"fail_create": "ix_probe"}, False)])
def test_build_lookup_index_answers_whether_the_index_is_valid(failure, answer, monkeypatch):
    from celerp.migrations._lookup_index import build_lookup_index
    op = _Op(["ix_probe"], **failure)
    monkeypatch.setattr("celerp.migrations._lookup_index.op", op)
    assert build_lookup_index("ix_probe", "projections", lambda: op.create_index("ix_probe")) is answer
