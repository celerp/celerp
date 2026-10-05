# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""What the chart, locations, taxes and payment terms import results show.

The four endpoints report incomplete success differently: locations returns a
number of failed rows, the others return the error messages. The result page
shows the messages whenever there are any, shows the failed-row count only when
that is all the endpoint said, and keeps the staged file in both cases so the
user can go back and retry.
"""

from __future__ import annotations

import ast
from unittest.mock import AsyncMock

import pytest

from celerp.services import import_stage

from test_import_stage_invariants import _IMPORTERS, _REPO, _calls, _confirm, _route_path
from ui.routes import csv_import as ci

_RESULT_IMPORTERS = ["chart", "locations", "taxes", "terms"]

_ERRORS_ONLY = {"created": 0, "skipped": 0, "errors": ["Zeta VAT: Invalid rate"]}
_FAILED_ONLY = {"created": 0, "skipped": 0, "failed": 2}
_BOTH = {"created": 0, "skipped": 0, "failed": 1, "errors": ["Zeta VAT: Invalid rate"]}
_CLEAN = {"created": 1, "skipped": 0, "errors": []}


@pytest.fixture
def stage_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("celerp.config.settings.data_dir", tmp_path)
    return tmp_path / "import_staging"


async def _result(name: str, result: dict):
    ref, r = await _confirm(name, AsyncMock(return_value=dict(result)))
    assert r.status_code == 200, r.text
    return ref, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_renders_actual_errors(name, stage_dir):
    _, html = await _result(name, _ERRORS_ONLY)
    assert "Zeta VAT: Invalid rate" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_failed_count_rendered_when_it_is_the_only_signal(name, stage_dir):
    _, html = await _result(name, _FAILED_ONLY)
    assert "2 record(s) failed" in html


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_failed_count_not_rendered_when_errors_are_present(name, stage_dir):
    _, html = await _result(name, _BOTH)
    assert "Zeta VAT: Invalid rate" in html
    assert "record(s) failed" not in html


def test_helper_accepts_errors_list_and_failed_count():
    assert ci.import_result_errors(_ERRORS_ONLY) == ["Zeta VAT: Invalid rate"]
    assert ci.import_result_errors(_FAILED_ONLY) == ["2 record(s) failed"]
    assert ci.import_result_errors(_BOTH) == ["Zeta VAT: Invalid rate"]
    assert ci.import_result_errors(_CLEAN) == []
    assert ci.import_result_errors({"created": 3, "failed": 0}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_keeps_stage_on_errors(name, stage_dir):
    ref, _ = await _result(name, _ERRORS_ONLY)
    assert import_stage.read_stage("company-a", ref) == _IMPORTERS[name]["csv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_keeps_stage_on_failed_count(name, stage_dir):
    ref, _ = await _result(name, _FAILED_ONLY)
    assert import_stage.read_stage("company-a", ref) == _IMPORTERS[name]["csv"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _RESULT_IMPORTERS)
async def test_every_importer_discards_stage_on_clean_result(name, stage_dir):
    ref, _ = await _result(name, _CLEAN)
    assert import_stage.read_stage("company-a", ref) is None


def test_four_confirm_routes_use_the_one_helper():
    """Each confirm route builds its result errors with the shared helper, never its own copy."""
    wanted = {_IMPORTERS[name]["confirm"] for name in _RESULT_IMPORTERS}
    seen = set()
    for path in ("accounting_import.py", "settings_import.py"):
        for node in ast.walk(ast.parse((_REPO / "ui" / "routes" / path).read_text(encoding="utf-8"))):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            routes = {p for p in map(_route_path, node.decorator_list) if p} & wanted
            if not routes:
                continue
            seen |= routes
            assert "import_result_errors" in _calls(node), f"{routes} builds its own result errors"
            assert "records_failed" not in ast.unparse(node), f"{routes} formats the failed count itself"
    assert seen == wanted


def test_every_result_panel_names_its_records_in_the_users_language():
    """The result's "View ..." button names the imported records through the catalog
    ("Lager anzeigen" in German), never an English literal."""
    callers = []
    for path in sorted((_REPO / "ui" / "routes").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and "import_result_panel" in ast.unparse(node.func):
                label = next(k.value for k in node.keywords if k.arg == "entity_label")
                callers.append((path.name, node.lineno, isinstance(label, ast.Constant)))
    assert callers and not [c for c in callers if c[2]], callers
