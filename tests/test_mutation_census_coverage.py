# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The census fails a writer no test covers: a mutating route whose source reaches a write
and that no test made write durably, and a write site no observed request write passed
through, unless the coverage-gap baseline (mutation_coverage_gaps.json) names it. The
baseline does not grow and holds no entry that no longer matches a gap."""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import mutation_baseline  # noqa: E402
import mutation_census  # noqa: E402

SOURCE = '''\
@router.post("/new")
async def create(session, row):
    session.add(row)
    await session.commit()


@router.post("/preview")
async def preview(data):
    return {"total": sum(data)}
'''
WRITER = "api POST /new"
SITE = "celerp/routers/x.py create add"


def _census(tmp_path, monkeypatch, routes=None, sites=None, gaps=()) -> tuple[int, str]:
    (tmp_path / "celerp" / "routers").mkdir(parents=True)
    (tmp_path / "celerp" / "routers" / "x.py").write_text(SOURCE)
    monkeypatch.setattr(mutation_census, "ROOT", str(tmp_path))
    monkeypatch.setattr(mutation_census.static_calls, "ROOT", str(tmp_path))
    folder = tmp_path / "census"
    folder.mkdir()
    inventory = [{"app": "FastAPI", "methods": ["POST"], "path": "/new", "source": "celerp/routers/x.py:1"},
                 {"app": "FastAPI", "methods": ["POST"], "path": "/preview", "source": "celerp/routers/x.py:7"}]
    (folder / "census-1-gw0.routes.json").write_text(json.dumps(inventory))
    (folder / "census-1-gw0.json").write_text(json.dumps({"routes": routes or {}, "sites": sites or {}, "findings": []}))
    (tmp_path / "baseline.json").write_text("[]")
    (tmp_path / "gaps.json").write_text(json.dumps(list(gaps)))
    status = mutation_census.main([str(folder), "--out", str(tmp_path / "census.md"), "--enforce",
                                   "--baseline", str(tmp_path / "baseline.json"), "--gaps", str(tmp_path / "gaps.json")])
    return status, (tmp_path / "census.md").read_text()


def _route_gap(route=WRITER, cls="never requested") -> dict:
    return {"route": route, "class": cls, "reason": "no test creates one yet"}


def _site_gap(site=SITE, count=1) -> dict:
    return {"site": site, "count": count, "class": "request write no test reaches", "reason": "no test creates one yet"}


WROTE = {WRITER: {"requests": 1, "protected": 1}}
PASSED = {"celerp/routers/x.py:3 create@1": 1}


def test_a_new_post_writer_no_test_requests_fails(tmp_path, monkeypatch):
    status, text = _census(tmp_path, monkeypatch)
    assert status == 1
    assert "| api | POST /new | celerp/routers/x.py:1 | never requested |" in text


def test_a_post_writer_a_test_requests_without_a_durable_write_fails(tmp_path, monkeypatch):
    status, text = _census(tmp_path, monkeypatch, routes={WRITER: {"requests": 2}}, sites=PASSED)
    assert status == 1
    assert "| api | POST /new | celerp/routers/x.py:1 | requested, no durable write |" in text


def test_a_write_site_no_request_write_passed_through_fails(tmp_path, monkeypatch):
    status, text = _census(tmp_path, monkeypatch, routes=WROTE)
    assert status == 1
    assert f"| {SITE} | 1 |" in text


def test_a_covered_writer_and_a_route_reaching_no_write_pass(tmp_path, monkeypatch):
    assert _census(tmp_path, monkeypatch, routes=WROTE, sites=PASSED)[0] == 0


def test_a_route_written_through_by_its_outer_request_is_covered(tmp_path, monkeypatch):
    """A UI route that writes by calling the API: the observer counts the write for it too."""
    routes = {WRITER: {"requests": 1, "writes through": 1}}
    assert _census(tmp_path, monkeypatch, routes=routes, sites=PASSED)[0] == 0


def test_gaps_the_baseline_names_pass(tmp_path, monkeypatch):
    status, text = _census(tmp_path, monkeypatch, gaps=[_route_gap(), _site_gap()])
    assert status == 0
    assert "| route | api POST /new | never requested | matched |" in text


@pytest.mark.parametrize("routes, sites, gaps, why", [
    (WROTE, PASSED, [_route_gap()], "stale: covered"),
    (WROTE, PASSED, [_site_gap()], "stale: covered"),
    (WROTE, PASSED, [_route_gap("api POST /gone")], "stale: no such gap"),
    (None, None, [_route_gap(cls="requested, no durable write"), _site_gap()], "class is never requested"),
    (WROTE, None, [_site_gap(count=2)], "1 site(s), entry says 2"),
], ids=["route-covered", "site-covered", "route-gone", "class-changed", "site-count"])
def test_a_baseline_entry_no_longer_matching_its_gap_fails(tmp_path, monkeypatch, routes, sites, gaps, why):
    status, text = _census(tmp_path, monkeypatch, routes=routes, sites=sites, gaps=gaps)
    assert status == 1
    assert why in text


@pytest.mark.parametrize("change, error", [
    ({"reason": ""}, "lacks"), ({"class": "untested"}, "class"), ({"route": None, "site": None}, "names no"),
])
def test_a_gap_entry_without_its_classification_is_refused(tmp_path, change, error):
    path = tmp_path / "gaps.json"
    path.write_text(json.dumps([{k: v for k, v in {**_route_gap(), **change}.items() if v is not None}]))
    with pytest.raises(ValueError, match=error):
        mutation_baseline.load_gaps(str(path))


def test_the_checked_in_coverage_gaps_load_and_do_not_grow():
    """The count only goes down: a gap closed by a test leaves the baseline in the same change.
    Raising it accepts a new untested writer, which is what the census exists to refuse."""
    assert len(mutation_baseline.load_gaps()) <= mutation_baseline.GAPS_CEILING
