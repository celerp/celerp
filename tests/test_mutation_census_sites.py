# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The census counts a write site as passed through when an observed durable write was
made on the site's own lines. The observer records the line a statement ran from and,
when they are called, the line that added, deleted or merged a row, which the later flush
or commit sends: a site its function never ran is never counted."""

from __future__ import annotations

import os
import sys

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
    return mutation_census.static_inventory()[1]


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
    site = next(s for s in mutation_census.static_inventory()[1] if s[0] == rel and s[1] == add)
    assert mutation_census._passed_through(site, {(add, "create_work_center", site[5][0])})
    assert not mutation_census._passed_through(site, {(commit, "create_work_center", site[5][0])})
