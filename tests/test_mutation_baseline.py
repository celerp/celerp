# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The baseline of accepted write-observer findings (mutation_baseline.py): an entry accepts
only the exact finding it names, and the census fails on a finding no entry accepts, an
entry no finding matched, and an entry its own reproducing test did not see."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict

import pytest
import mutation_baseline
import mutation_observer
from mutation_observer import serving
from celerp.events.engine import emit_event
from celerp.services.permissions import authorize_request
from scripts import mutation_census
from test_mutation_census_coverage import committed_gaps

pytestmark = [pytest.mark.observer_control]

_TEST = "tests/test_mutation_baseline.py::reproducing"


def _entry(finding: dict, **changes) -> dict:
    entry = {name: finding[name] for name in ("route", "kind", "table", "fingerprint")}
    entry.update(where=[mutation_baseline._unlined(frame) for frame in finding["where"]], test=_TEST,
                 issue="https://example.invalid/issues/1", owner="celerp-core",
                 remove_when="the write takes the company lock")
    return {**entry, **changes}


async def _unlocked_event(session, auth, route: str) -> list[dict]:
    """An event emitted without the company lock: its ledger and projection writes."""
    mutation_observer.reset()
    with serving("POST", route):
        cid = auth["company_id"]
        authorize_request(session, cid, auth["user_id"], "admin")
        await emit_event(session, company_id=cid, entity_id=f"contact:{uuid.uuid4()}", entity_type="contact",
                         event_type="crm.contact.created", data={"name": "Observed", "contact_type": "both"},
                         actor_id=None, location_id=None, source="test",
                         idempotency_key=f"test:{uuid.uuid4()}", metadata_={})
        await session.commit()
    return [asdict(f) for f in mutation_observer.CENSUS.findings]


@pytest.mark.asyncio
async def test_an_entry_accepts_only_the_exact_write_it_names(session, auth, monkeypatch):
    first, *others = await _unlocked_event(session, auth, "/observed")
    assert first["where"] and others
    monkeypatch.setattr(mutation_baseline, "ENTRIES", {mutation_baseline.key(first): _entry(first)})
    assert mutation_baseline.accepted(first)
    # The route's other writes (another table or statement shape) are not accepted.
    assert not any(mutation_baseline.accepted(f) for f in others)
    # The same write from another route is not.
    elsewhere = await _unlocked_event(session, auth, "/elsewhere")
    assert [f["table"] for f in elsewhere][0] == first["table"]
    assert not any(mutation_baseline.accepted(f) for f in elsewhere)
    # The same write reached through another app frame is not either.
    moved = {**first, "where": first["where"][:-1] + ["celerp/other.py:1 other"]}
    assert not mutation_baseline.accepted(moved)
    # Line numbers alone do not count: a line moved by an unrelated edit still matches.
    relined = {**first, "where": [f.replace(" ", "9 ", 1) for f in first["where"]]}
    assert relined["where"] != first["where"] and mutation_baseline.accepted(relined)


def _finding(route: str = "api POST /observed", tests=(_TEST,)) -> dict:
    return {"kind": "unprotected", "route": route, "table": "locations", "fingerprint": "f1",
            "where": ["celerp/a.py:10 write"], "count": 1, "tests": list(tests), "key": route,
            "detail": "", "earlier_locks": [], "company_from": "statement", "sql": "UPDATE locations"}


def _census(tmp_path, findings, entries) -> int:
    folder = tmp_path / "census"
    folder.mkdir()
    (folder / "census-1-gw0.json").write_text(json.dumps({"routes": {}, "sites": {}, "findings": findings}))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(entries))
    gaps = committed_gaps(tmp_path / "gaps.json")
    with pytest.MonkeyPatch.context() as patch:  # findings alone: no source, so no coverage gap
        patch.setattr(mutation_census, "static_inventory", lambda graph: ([], []))
        return mutation_census.main([str(folder), "--out", str(tmp_path / "census.md"),
                                     "--baseline", str(baseline), "--gaps", gaps, "--base", "HEAD", "--enforce"])


def test_the_census_passes_when_every_finding_matches_one_entry(tmp_path):
    assert _census(tmp_path, [_finding()], [_entry(_finding())]) == 0
    assert "| matched |" in (tmp_path / "census.md").read_text()


def test_an_entry_is_reproduced_by_its_test_under_any_parameters(tmp_path):
    """pytest-playwright parametrizes a browser test by browser in some versions, as [chromium]."""
    assert _census(tmp_path, [_finding(tests=[f"{_TEST}[chromium]"])], [_entry(_finding())]) == 0
    assert "| matched |" in (tmp_path / "census.md").read_text()


@pytest.mark.parametrize("findings, entries, why", [
    ([_finding("api POST /new")], [], "a finding no entry accepts"),
    ([], [_entry(_finding())], "stale: no finding matched"),
    ([_finding(tests=["tests/other.py::t"])], [_entry(_finding())], "not reproduced by its test"),
])
def test_the_census_fails_on(tmp_path, findings, entries, why):
    assert _census(tmp_path, findings, entries) == 1
    if why != "a finding no entry accepts":
        assert why in (tmp_path / "census.md").read_text()


@pytest.mark.parametrize("change", [{"issue": ""}, {"test": ""}, {"owner": ""}, {"remove_when": ""}])
def test_an_entry_without_its_tracking_fields_is_refused(tmp_path, change):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps([_entry(_finding(), **change)]))
    with pytest.raises(ValueError, match="lacks"):
        mutation_baseline.load(str(path))


def test_an_entry_naming_a_parametrized_test_is_refused(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps([_entry(_finding(), test=f"{_TEST}[chromium]")]))
    with pytest.raises(ValueError, match="parametrized"):
        mutation_baseline.load(str(path))


def test_two_entries_for_one_finding_are_refused(tmp_path):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps([_entry(_finding()), _entry(_finding(), issue="https://example.invalid/2")]))
    with pytest.raises(ValueError, match="two baseline entries"):
        mutation_baseline.load(str(path))


def test_the_checked_in_baseline_loads():
    assert mutation_baseline.load() == mutation_baseline.ENTRIES
