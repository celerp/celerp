# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for the undefined-name checker's scope model.

These pin the semantics directly, so the guard cannot silently regress into
missing real NameErrors (a false negative here would let another
undefined-reference bug merge while the repo-wide guard reports clean).
"""

import importlib.util
from pathlib import Path

_CHECKER_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_undefined_names.py"
_spec = importlib.util.spec_from_file_location("check_undefined_names", _CHECKER_PATH)
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)


def _names(src: str) -> set[str]:
    return {name for _lineno, name in _checker.check_source(src)}


# --- must REPORT (real NameErrors) ---

def test_class_attr_not_visible_in_method() -> None:
    src = (
        "class C:\n"
        "    x = 1\n"
        "    def f(self):\n"
        "        return x\n"
    )
    assert "x" in _names(src)


def test_unassigned_global_is_reported() -> None:
    src = (
        "def f():\n"
        "    global missing\n"
        "    return missing\n"
    )
    assert "missing" in _names(src)


def test_global_shadowed_by_enclosing_function_still_reported() -> None:
    src = (
        "def outer():\n"
        "    X = 1\n"
        "    def inner():\n"
        "        global X\n"
        "        return X\n"
    )
    assert "X" in _names(src)


def test_bare_undefined_name_is_reported() -> None:
    assert "nope" in _names("def f():\n    return nope\n")


# --- must NOT report (valid Python) ---

def test_class_attr_via_self_is_fine() -> None:
    src = (
        "class C:\n"
        "    x = 1\n"
        "    def f(self):\n"
        "        return self.x\n"
    )
    assert not _names(src)


def test_class_body_sees_its_own_names() -> None:
    src = (
        "class C:\n"
        "    x = 1\n"
        "    y = x + 1\n"
    )
    assert not _names(src)


def test_assigned_global_resolves() -> None:
    src = (
        "def setter():\n"
        "    global CONF\n"
        "    CONF = 1\n"
        "def getter():\n"
        "    global CONF\n"
        "    return CONF\n"
    )
    assert not _names(src)


def test_module_global_visible_via_global_decl() -> None:
    src = (
        "TOP = 1\n"
        "def f():\n"
        "    global TOP\n"
        "    return TOP\n"
    )
    assert not _names(src)


def test_nonlocal_binding_resolves() -> None:
    src = (
        "def outer():\n"
        "    v = 1\n"
        "    def inner():\n"
        "        nonlocal v\n"
        "        return v\n"
    )
    assert not _names(src)


def test_closure_over_enclosing_function_var() -> None:
    src = (
        "def outer():\n"
        "    v = 1\n"
        "    def inner():\n"
        "        return v\n"
    )
    assert not _names(src)


def test_comprehension_target_resolves() -> None:
    assert not _names("def f(xs):\n    return [i for i in xs]\n")


def test_builtins_resolve() -> None:
    assert not _names("def f(xs):\n    return len(list(xs))\n")
