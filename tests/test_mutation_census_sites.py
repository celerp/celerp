# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The census counts a write site as passed through when an observed durable write was
made on the site's own lines. The observer records the line a statement ran from and,
when they are called, the line that added, deleted or merged a row, which the later flush
or commit sends: a site its function never ran is never counted."""

from __future__ import annotations

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import mutation_census  # noqa: E402

SOURCE = '''\
async def save(session, row, old, other):
    session.add(row)
    session.delete(old)
    await session.merge(other)
    await emit_event(session, company_id=1)
    await session.commit()


class Other:
    async def save(self, session, row):
        session.add(row)
        await session.commit()


def outer(session):
    def inner(row):
        session.add(row)
        session.flush()
    return inner
'''


def _sites(tmp_path, monkeypatch):
    (tmp_path / "celerp").mkdir()
    (tmp_path / "celerp" / "w.py").write_text(SOURCE)
    monkeypatch.setattr(mutation_census, "ROOT", str(tmp_path))
    monkeypatch.setattr(mutation_census.static_calls, "ROOT", str(tmp_path))
    return mutation_census.static_inventory(mutation_census.static_calls.Graph())[1]


def _passed(sites, *sent):
    sent = {(line, name, first) for line, name, first in sent}
    return {(s[1], s[4]) for s in sites if mutation_census._passed_through(s, sent)}


def test_a_write_counts_only_the_site_it_was_made_on(tmp_path, monkeypatch):
    sites = _sites(tmp_path, monkeypatch)
    assert _passed(sites, (2, "save", 1), (5, "save", 1)) == {(2, "add"), (5, "emit_event")}


def test_a_commit_that_sends_rows_counts_no_site_by_itself(tmp_path, monkeypatch):
    sites = _sites(tmp_path, monkeypatch)
    assert _passed(sites, (6, "save", 1)) == set()


def test_a_function_of_the_same_name_is_told_apart_by_its_first_line(tmp_path, monkeypatch):
    sites = _sites(tmp_path, monkeypatch)
    assert _passed(sites, (11, "save", 10)) == {(11, "add")}
    assert _passed(sites, (11, "save", 1)) == set()


def test_a_nested_functions_write_counts_for_its_own_sites(tmp_path, monkeypatch):
    sites = _sites(tmp_path, monkeypatch)
    assert _passed(sites, (17, "inner", 16)) == {(17, "add")}


def test_a_driven_add_site_of_the_bundled_source_counts_on_its_own_line(tmp_path):
    """A work center is added on one line and sent at the commit after it: the observer
    records the add's line, and the commit's line alone counts nothing."""
    rel = "default_modules/celerp-manufacturing/celerp_manufacturing/routes.py"
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    add = next(i for i, line in enumerate(lines, 1) if line.strip() == "session.add(wc)")
    commit = next(i for i in range(add + 1, add + 6) if "await session.commit()" in lines[i - 1])
    site = next(s for s in mutation_census.static_inventory(mutation_census.static_calls.Graph())[1] if s[0] == rel and s[1] == add)
    assert mutation_census._passed_through(site, {(add, "create_work_center", site[5][0])})
    assert not mutation_census._passed_through(site, {(commit, "create_work_center", site[5][0])})


# A covered route gains a branch whose write no test drives: the census fails it as a
# write site no request write passed through, whatever form the write is spelled in.
COVERED = '''\
@router.post("/new")
async def create(session: AsyncSession, row, flag):
    session.add(row)
'''
BRANCHES = {
    "chained update": "        await session.execute(update(Item).where(Item.id == row.id).values(name='x'))\n",
    "chained delete": "        await session.execute(delete(Item).where(Item.id == row.id))\n",
    "statement variable": "        stmt = update(Item).values(name='x')\n        await session.execute(stmt)\n",
    "text sql": "        await session.execute(text('UPDATE items SET name = 1'))\n",
    "renamed session": "        db = session\n        db.add(row)\n",
    "unknown receiver": "        db.add(row)\n",
}


@pytest.mark.parametrize("branch", list(BRANCHES))
def test_an_unexercised_write_branch_of_a_covered_route_fails_the_census(tmp_path, monkeypatch, branch):
    source = "from sqlalchemy import delete, text, update\nfrom sqlalchemy.ext.asyncio import AsyncSession\n\n" \
        + COVERED + "    if flag:\n" + BRANCHES[branch] + "    await session.commit()\n"
    (tmp_path / "celerp" / "routers").mkdir(parents=True)
    (tmp_path / "celerp" / "routers" / "x.py").write_text(source)
    monkeypatch.setattr(mutation_census, "ROOT", str(tmp_path))
    monkeypatch.setattr(mutation_census.static_calls, "ROOT", str(tmp_path))
    folder = tmp_path / "census"
    folder.mkdir()
    (folder / "census-1-gw0.routes.json").write_text(json.dumps(
        [{"app": "FastAPI", "methods": ["POST"], "path": "/new", "source": "celerp/routers/x.py:4"}]))
    # a test requested POST /new and its add on line 6 was written: the covered path
    (folder / "census-1-gw0.json").write_text(json.dumps({
        "routes": {"api POST /new": {"requests": 1, "protected": 1}},
        "sites": {"celerp/routers/x.py:6 create@4": 1}, "findings": []}))
    (tmp_path / "b.json").write_text("[]")
    (tmp_path / "g.json").write_text("[]")
    status = mutation_census.main([str(folder), "--out", str(tmp_path / "c.md"), "--enforce",
                                   "--baseline", str(tmp_path / "b.json"), "--gaps", str(tmp_path / "g.json")])
    text = (tmp_path / "c.md").read_text()
    assert status == 1, text.split("## Mutation sites")[1][:400]
    assert "Write sites in the bundled source: 2;" in text


def test_every_session_call_of_the_bundled_source_is_classified():
    """Each execute, add, add_all, delete and merge call under celerp/, default_modules/ and
    ui/ is a write or matches a named read rule. A call the classifier cannot read fails
    here, so a new spelling of a write is never counted as no write."""
    found = mutation_census.static_calls.Graph().unclassified()
    assert not found, f"{len(found)} calls neither a write nor a named read:\n" + "\n".join(found[:80])


RULES = '''\
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession


async def run(session: AsyncSession, stmt, names: set[str]):
    seen = set()
    seen.add(1)
    names.add("x")
    await session.execute(select(Item).where(Item.id == 1))
    await session.execute(text("SELECT 1"))
    await session.execute(text("WITH moved AS (DELETE FROM items RETURNING id) SELECT id FROM moved"))
    await session.execute(stmt)
    query = select(Item)
    query = query.where(Item.id == 2)
    await session.execute(query)
'''


def test_the_classifier_reads_what_is_executed(tmp_path, monkeypatch):
    (tmp_path / "celerp").mkdir()
    (tmp_path / "celerp" / "r.py").write_text(RULES)
    monkeypatch.setattr(mutation_census.static_calls, "ROOT", str(tmp_path))
    graph = mutation_census.static_calls.Graph()
    assert dict(graph.functions["celerp.r.run"].effects) == {11: "write delete", 12: "write unclassified execute"}
    assert graph.unclassified() == ["celerp/r.py:12 execute"]
