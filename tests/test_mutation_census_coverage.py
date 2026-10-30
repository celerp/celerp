# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The census fails a writer no test covers: a mutating route whose source reaches a write
and that no test made write durably, and a write site no observed request write passed
through, unless the coverage-gap baseline (mutation_coverage_gaps.json) names it. The
baseline does not grow, holds no entry that no longer matches a gap, and holds no entry the
base branch's copy does not, nor a site entry counting more sites than its copy there: a change
may drop entries or lower a count, never add, swap in or raise one."""

from __future__ import annotations

import json
import os
import subprocess
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


def committed_gaps(path, entries=(), base_entries=None) -> str:
    """Writes *entries* to the gaps file at *path* over a commit of *base_entries* (the same
    entries when not given) in a new repository at its folder, so the census reads the base
    branch's list at HEAD."""
    git = ["git", "-C", str(path.parent), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    path.write_text(json.dumps(list(entries if base_entries is None else base_entries)))
    subprocess.run([*git, "add", path.name], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    path.write_text(json.dumps(list(entries)))
    return str(path)


def _census(tmp_path, monkeypatch, routes=None, sites=None, gaps=(), base_gaps=None, source=SOURCE) -> tuple[int, str]:
    """Runs the census over *source* with *gaps* checked out over a base commit holding
    *base_gaps* (the same entries when not given)."""
    (tmp_path / "celerp" / "routers").mkdir(parents=True)
    (tmp_path / "celerp" / "routers" / "x.py").write_text(source)
    monkeypatch.setattr(mutation_census, "ROOT", str(tmp_path))
    monkeypatch.setattr(mutation_census.static_calls, "ROOT", str(tmp_path))
    folder = tmp_path / "census"
    folder.mkdir()
    inventory = [{"app": "FastAPI", "methods": ["POST"], "path": "/new", "source": "celerp/routers/x.py:1"},
                 {"app": "FastAPI", "methods": ["POST"], "path": "/preview", "source": "celerp/routers/x.py:7"}]
    (folder / "census-1-gw0.routes.json").write_text(json.dumps(inventory))
    (folder / "census-1-gw0.json").write_text(json.dumps({"routes": routes or {}, "sites": sites or {}, "findings": []}))
    (tmp_path / "baseline.json").write_text("[]")
    status = mutation_census.main([str(folder), "--out", str(tmp_path / "census.md"), "--enforce",
                                   "--baseline", str(tmp_path / "baseline.json"),
                                   "--gaps", committed_gaps(tmp_path / "gaps.json", gaps, base_gaps), "--base", "HEAD"])
    return status, (tmp_path / "census.md").read_text()


def _route_gap(route=WRITER, cls="never requested") -> dict:
    return {"route": route, "class": cls, "reason": "no test creates one yet"}


def _site_gap(site=SITE, count=1) -> dict:
    return {"site": site, "count": count, "class": "reached from a route", "reason": "no test creates one yet"}


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


# A route whose only write is setting a loaded row's attribute has no write site: it reaches a
# write through its commit, however the commit is spelled.
RENAMES = '''\
@router.post("/new")
async def create(session: AsyncSession, row_id):
    row = await session.get(Item, row_id)
    row.name = "renamed"
    COMMIT

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
SessionLocal = async_sessionmaker()
'''
FACTORY_BEGIN = '''\
@router.post("/new")
async def create(row_id):
    async with SessionLocal.begin() as session:
        row = await session.get(Item, row_id)
        row.name = "renamed"

from sqlalchemy.ext.asyncio import async_sessionmaker
SessionLocal = async_sessionmaker()
'''
COMMIT_SPELLINGS = {
    "called": RENAMES.replace("COMMIT", "await session.commit()"),
    "aliased": RENAMES.replace("COMMIT", "done = session.commit\n    await done()"),
    "getattr": RENAMES.replace("COMMIT", "await getattr(session, 'commit')()"),
    "sync session": RENAMES.replace("COMMIT", "session.sync_session.commit()"),
    "session begin block": RENAMES.replace("    row = await session.get(Item, row_id)\n    row.name = \"renamed\"\n    COMMIT",
                                           "    async with session.begin():\n        row = await session.get(Item, row_id)\n"
                                           "        row.name = \"renamed\""),
    "factory begin block": FACTORY_BEGIN,
}


@pytest.mark.parametrize("source", COMMIT_SPELLINGS.values(), ids=COMMIT_SPELLINGS.keys())
def test_a_route_reaches_a_write_through_its_commit_however_it_is_spelled(tmp_path, monkeypatch, source):
    status, text = _census(tmp_path, monkeypatch, source=source)
    assert status == 1
    assert "| api | POST /new | celerp/routers/x.py:1 | never requested |" in text


OTHER = "api POST /other"


def test_a_gap_entry_the_base_branch_does_not_hold_fails(tmp_path, monkeypatch):
    status, text = _census(tmp_path, monkeypatch, gaps=[_route_gap(), _site_gap()], base_gaps=[_site_gap()])
    assert status == 1
    assert "| route | api POST /new | never requested | not in the list at HEAD |" in text


def test_a_newly_untested_route_swapped_in_for_an_accepted_one_fails(tmp_path, monkeypatch):
    """The count stays the same, so only the comparison with the base branch catches it."""
    status, text = _census(tmp_path, monkeypatch, gaps=[_route_gap(), _site_gap()],
                           base_gaps=[_route_gap(OTHER), _site_gap()])
    assert status == 1
    assert "| route | api POST /new | never requested | not in the list at HEAD |" in text


def test_a_site_entry_counting_more_sites_than_the_base_branch_holds_fails(tmp_path, monkeypatch):
    """A new untested write site in a function with an accepted site gap, with the entry's count raised."""
    source = SOURCE.replace("    session.add(row)\n", "    session.add(row)\n    session.add(row)\n", 1)
    status, text = _census(tmp_path, monkeypatch, routes=WROTE, gaps=[_site_gap(count=2)],
                           base_gaps=[_site_gap(count=1)], source=source)
    assert status == 1
    assert "| site | celerp/routers/x.py create add | reached from a route | 2 site(s), the list at HEAD holds 1 |" in text


def test_dropping_an_entry_the_base_branch_holds_passes(tmp_path, monkeypatch):
    status, text = _census(tmp_path, monkeypatch, routes=WROTE, gaps=[_site_gap()],
                           base_gaps=[_route_gap(), _site_gap()])
    assert status == 0
    assert "| site | celerp/routers/x.py create add | reached from a route | matched |" in text


def test_a_base_without_the_gaps_file_compares_nothing(tmp_path, monkeypatch):
    """The change that brings the list in has no earlier copy to compare with."""
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    (tmp_path / "gaps.json").write_text(json.dumps([_route_gap()]))
    assert mutation_baseline.gaps_at("HEAD", str(tmp_path / "gaps.json")) is None
    with pytest.raises(subprocess.CalledProcessError):
        mutation_baseline.gaps_at("no-such-ref", str(tmp_path / "gaps.json"))


def test_the_checked_in_coverage_gaps_load_and_do_not_grow():
    """The count only goes down: a gap closed by a test leaves the baseline in the same change.
    Raising it accepts a new untested writer, which is what the census exists to refuse."""
    assert len(mutation_baseline.load_gaps()) <= mutation_baseline.GAPS_CEILING
