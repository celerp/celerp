# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Guard against undefined names (NameError at runtime) that the linters miss.

Every UI route and component does `from fasthtml.common import *`, which disables
pyflakes/ruff F821 undefined-name reporting for that whole layer. Issue 284 (a
route referencing a constant that did not exist) shipped through that blind spot.
This test runs the star-import-aware checker over ui/, celerp/, and scripts/ and
fails on any undefined load, so the class cannot regress.
"""

import importlib.util
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECKER_PATH = _REPO_ROOT / "scripts" / "check_undefined_names.py"

_spec = importlib.util.spec_from_file_location("check_undefined_names", _CHECKER_PATH)
_checker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_checker)


def test_no_undefined_names_in_repo() -> None:
    roots = [str(_REPO_ROOT / pkg) for pkg in ("ui", "celerp", "scripts")]
    findings = _checker.find_undefined(roots)
    detail = "\n".join(
        f"{path.relative_to(_REPO_ROOT)}:{lineno}: undefined name '{name}'"
        for path, lineno, name in findings
    )
    assert not findings, f"undefined names found:\n{detail}"
