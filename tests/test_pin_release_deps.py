# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The release pinning script turns pyproject ranges into the exact tested set."""

import importlib.util
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pin_release_deps", _ROOT / "scripts" / "pin_release_deps.py")
_pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pin)


def _pinned() -> dict:
    text = _pin.pin(
        (_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        (_ROOT / "constraints.txt").read_text(encoding="utf-8"),
    )
    return tomllib.loads(text)["project"]


def _names(requirements: list[str]) -> set[str]:
    return {_pin._req_name(r) for r in requirements}


def test_every_dependency_is_an_exact_pin() -> None:
    deps = _pinned()["dependencies"]
    assert deps
    for requirement in deps:
        spec = requirement.split(";")[0]
        assert "==" in spec and not any(op in spec for op in (">", "<", "~", "!")), requirement


def test_every_declared_dependency_is_present() -> None:
    declared = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert _names(declared["dependencies"]) <= _names(_pinned()["dependencies"])


def test_transitive_dependencies_are_pinned() -> None:
    names = _names(_pinned()["dependencies"])
    # starlette comes in only through fastapi; greenlet only through sqlalchemy.
    assert "starlette" in names


def test_markers_are_kept() -> None:
    deps = {_pin._req_name(r): r for r in _pinned()["dependencies"]}
    assert "python_full_version < '3.11'" in deps["tomli"]
    pg = deps["celerp-postgres"]
    assert "sys_platform == 'linux'" in pg and "sys_platform == 'win32'" in pg
    assert "darwin" not in pg


def test_every_marker_line_of_a_package_is_kept() -> None:
    # Resolved per Python version: dropping one leaves that version unpinned.
    deps = _pinned()["dependencies"]
    for name, count in (("numpy", 3), ("sqlalchemy", 2), ("websockets", 2)):
        constraint_lines = [
            line for line in (_ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and _pin._req_name(line) == name
        ]
        assert len(constraint_lines) == count
        assert [r for r in deps if _pin._req_name(r) == name] == constraint_lines


def test_pinned_set_equals_the_constraint_lines_it_covers() -> None:
    lines = [line for line in (_ROOT / "constraints.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    deps = _pinned()["dependencies"]
    assert len(deps) == len(set(deps))
    assert set(deps) == {line for line in lines if _pin._req_name(line) in _names(deps)}


def test_resolved_extra_is_pinned_and_dev_extra_left_alone() -> None:
    project = _pinned()
    assert project["optional-dependencies"]["prod"] == [
        r for r in project["optional-dependencies"]["prod"] if r.startswith("gunicorn==")
    ]
    declared = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["optional-dependencies"]["dev"] == declared["optional-dependencies"]["dev"]


def test_pip_and_setuptools_are_never_pinned() -> None:
    names = _names(_pinned()["dependencies"])
    assert "pip" not in names and "setuptools" not in names


def test_idempotent() -> None:
    constraints = (_ROOT / "constraints.txt").read_text(encoding="utf-8")
    once = _pin.pin((_ROOT / "pyproject.toml").read_text(encoding="utf-8"), constraints)
    assert _pin.pin(once, constraints) == once
