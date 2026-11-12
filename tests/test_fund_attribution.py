# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every journal producer names the fund its lines carry, and every emitter whose event type is
not a literal says why it never posts a journal entry: a new posting path or a new variable
emitter cannot ship unclassified."""

from __future__ import annotations

import pathlib

import fund_census
from celerp.services.fund_attribution import FUND_ATTRIBUTION, NON_JOURNAL_EMITTERS, RULE_CLASSES


def _classified(producers: set[str], variable: set[str]) -> dict[str, list[str]]:
    found = producers | variable
    return {
        "unclassified producer": sorted(producers - set(FUND_ATTRIBUTION)),
        "variable emitter without a reason": sorted(variable - set(FUND_ATTRIBUTION) - set(NON_JOURNAL_EMITTERS)),
        "orphan row": sorted((set(FUND_ATTRIBUTION) | set(NON_JOURNAL_EMITTERS)) - found),
        "producer declared non-journal": sorted(producers & set(NON_JOURNAL_EMITTERS)),
    }


def test_every_journal_producer_has_attribution():
    problems = _classified(*fund_census.census())
    assert problems == {k: [] for k in problems}
    assert set(FUND_ATTRIBUTION) & set(NON_JOURNAL_EMITTERS) == set()
    assert {rule for rule in FUND_ATTRIBUTION.values()} <= set(RULE_CLASSES)
    assert all(reason.strip() for reason in NON_JOURNAL_EMITTERS.values())


def test_a_new_producer_or_variable_emitter_is_named(tmp_path: pathlib.Path):
    package = tmp_path / "celerp" / "services"
    package.mkdir(parents=True)
    (package / "sneaky.py").write_text(
        "async def post(session, rec):\n"
        "    await emit_event(session, event_type='acc.journal_entry.created', data={})\n"
        "async def relay(session, rec):\n"
        "    await emit_event(session, event_type=rec.event_type, data={})\n"
        "async def plain(session):\n"
        "    await emit_event(session, event_type='item.updated', data={})\n",
        encoding="utf-8")
    producers, variable = fund_census.census(tmp_path, roots=("celerp",))
    assert (producers, variable) == ({"celerp.services.sneaky:post"}, {"celerp.services.sneaky:relay"})
    problems = _classified(producers, variable)
    assert problems["unclassified producer"] == ["celerp.services.sneaky:post"]
    assert problems["variable emitter without a reason"] == ["celerp.services.sneaky:relay"]
