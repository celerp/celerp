# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""The event and projection engines serve requests, imports, replay and internal work,
each with its own transaction. They observe the company lock and may refuse work that
lacks it; they never take it, so the operation that owns a write takes the lock at its
own boundary, before any narrower lock it holds (company_lock.lock_company).

The guard follows every call the source resolves (scripts/static_calls.py), from every
function of the engines to the functions that take the lock themselves, so a lock taken
through any number of wrappers is seen. It reads the source rather than a run, so it sees
the paths no test drives too."""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import static_calls  # noqa: E402

ENGINES = ("celerp/projections/", "celerp/events/engine.py")
# The functions the engines reach that take a company's lock themselves ("<file> <function>").
# Each predates this rule and moves to the owning writers' boundary in its own follow-up.
ALLOWED = {
    "celerp/events/engine.py emit_event": "the lock before a change to an existing item is applied",
    "celerp/services/physical_codes.py lock_item_code_namespace": "an item's code checked against the codes in use",
    "celerp/services/journal_reversals.py require_cancelled_once": "a cancel refused once its journal is reversed",
    "celerp/services/document_lines.py linked_items": "the items a document's lines link, read for its event",
    "celerp/services/auto_je.py refuse_stranding_set_aside": "a set-aside lot refused when a change strands it",
}


def _is_engine(fn) -> bool:
    return fn.path.startswith(ENGINES)


def locks_reached(graph: static_calls.Graph) -> dict[str, list]:
    """Each function the engines reach that takes the lock itself, with one chain to it."""
    starts = [key for key, fn in graph.functions.items() if _is_engine(fn)]
    found = {}
    for chain in graph.doing(starts, lambda what: what.startswith("lock ")):
        locker = graph.functions[chain[-1][0]]
        found.setdefault(f"{locker.path} {locker.qualname}", chain)
    return found


_GRAPH = None


def _graph() -> static_calls.Graph:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = static_calls.Graph()
    return _GRAPH


def test_no_engine_takes_a_company_lock():
    found = locks_reached(_graph())
    taken = [f"{locker}: {static_calls.describe(chain)}" for locker, chain in sorted(found.items())
             if locker not in ALLOWED]
    assert not taken, "an engine takes the company lock; its caller must take it instead:\n" + "\n".join(taken)


def test_every_allowed_lock_is_still_reached():
    """An allowance its code no longer needs is removed with it."""
    assert set(ALLOWED) <= set(locks_reached(_graph()))


def test_the_guard_sees_a_lock_taken_through_wrappers(tmp_path, monkeypatch):
    (tmp_path / "celerp" / "projections").mkdir(parents=True)
    (tmp_path / "celerp" / "services").mkdir(parents=True)
    (tmp_path / "celerp" / "projections" / "x.py").write_text(
        "from celerp.services.y import outer\n\nasync def replay(s, c):\n    await outer(s, c)\n")
    (tmp_path / "celerp" / "services" / "y.py").write_text(
        "from celerp.services import z\n\nasync def outer(s, c):\n    await z.inner(s, c)\n")
    (tmp_path / "celerp" / "services" / "z.py").write_text(
        "async def inner(s, c):\n    await lock_company(s, c)\n")
    monkeypatch.setattr(static_calls, "ROOT", str(tmp_path))
    found = locks_reached(static_calls.Graph())
    assert list(found) == ["celerp/services/z.py inner"]
    assert static_calls.describe(found["celerp/services/z.py inner"]) == \
        "replay:4 outer -> outer:4 inner -> inner:2 lock lock_company"
