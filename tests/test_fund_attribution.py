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


# What enumerating a record's live family looks like in source: the family readers, or a scan of
# the record's entry ids by prefix (which takes in every copy).
_FAMILY_READERS = frozenset({"_doc_recognition_jes", "restore_ids", "void_record_postings", "repost_record_postings",
                             "imported_document", "_doc_void_events", "_doc_receipt_booked", "_doc_receipt_jes",
                             "_sum_lines", "startswith"})


def _undo_problems(found: dict[str, set[str]], table: dict[str, str]) -> dict[str, list[str]]:
    return {
        "unlisted undo": sorted(set(found) - set(table)),
        "orphan row": sorted(set(table) - set(found)),
        "current_entry not called": sorted(n for n, how in table.items()
                                           if how == "current_entry" and "current_entry" not in found.get(n, ())),
        "family not enumerated": sorted(n for n, how in table.items()
                                        if how == "family" and not found.get(n, set()) & _FAMILY_READERS),
        "no reason": sorted(n for n, how in table.items()
                            if how not in ("current_entry", "family") and not how.startswith("never: ")),
    }


def test_every_journal_undo_resolves_current_entry():
    from celerp.services.fund_attribution import UNDO_LIVE_ENTRY

    reversals = {n for n, rule in FUND_ATTRIBUTION.items() if rule in ("V", "X")}
    found = fund_census.undo_census(extra=reversals)
    problems = _undo_problems(found, UNDO_LIVE_ENTRY)
    assert problems == {k: [] for k in problems}


def test_a_new_undo_reading_a_fixed_id_is_named(tmp_path: pathlib.Path):
    package = tmp_path / "celerp" / "services"
    package.mkdir(parents=True)
    (package / "sneaky.py").write_text(
        "async def undo(session, doc_id):\n"
        "    await emit_event(session, entity_id=f'je:auto:{doc_id}:fin', event_type='acc.journal_entry.voided')\n"
        "async def live(session, cid, doc_id):\n"
        "    je_id = await current_entry(session, cid, f'je:auto:{doc_id}:fin')\n"
        "    await emit_event(session, entity_id=je_id, event_type='acc.journal_entry.voided')\n",
        encoding="utf-8")
    found = fund_census.undo_census(tmp_path, roots=("celerp",))
    assert set(found) == {"celerp.services.sneaky:undo", "celerp.services.sneaky:live"}
    problems = _undo_problems(found, {"celerp.services.sneaky:undo": "current_entry",
                                      "celerp.services.sneaky:live": "current_entry"})
    assert problems["current_entry not called"] == ["celerp.services.sneaky:undo"]
    assert _undo_problems(found, {"celerp.services.sneaky:live": "current_entry"})["unlisted undo"] == [
        "celerp.services.sneaky:undo"]
