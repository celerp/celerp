# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""History records a change exactly when the agreement's page would show one. The fields
come from the census of what the module's writers record (history_census.py), so a field
added to a writer is driven here without anyone listing it."""
from __future__ import annotations

import json
import uuid

from bs4 import BeautifulSoup
from sqlalchemy import func, select

import history_census
from agreement_kit import agreement, company_id, freeze_day
from test_history_and_lists import _changed_agreement


def test_every_field_an_agreement_event_records_is_named_with_its_unset_value():
    from celerp_subscriptions.periods import UNSET

    rows = history_census.fields()
    assert not [f for f, r in rows.items() if r.kind == "unnamed"]
    set_anywhere = {f for w in history_census.writes() for f in w.sets} | set(history_census.DERIVED)
    assert not sorted(set_anywhere - set(UNSET))


def test_no_change_history_shows_nothing_of_is_recorded_alone():
    assert history_census.companions_alone() == []


def test_every_agreement_writer_records_what_changes_through_the_one_rule():
    assert history_census.moves_not_through_changed_fields() == []


def _values(changes: list[dict], state: dict, f: str, unset) -> list:
    """Every value ``f`` takes in a real agreement's life, with None and its unset value."""
    seen = [None, unset, state.get(f)]
    for c in changes:
        if isinstance(c.get(f), dict):
            seen += [c[f].get("old"), c[f].get("new")]
    unique = {json.dumps(v, sort_keys=True, default=str): v for v in seen}
    return list(unique.values())


def _describer(f: str):
    """How History renders one change of ``f``: its describer, else the generic field line."""
    from ui.components import activity

    return activity._FIELD_DESCRIBERS.get(f) or (lambda c: activity._fields_changed_summary({f: c}))


async def _owner_id(ui) -> uuid.UUID:
    users = (await ui.api.get("/companies/me/users")).json()["items"]
    return uuid.UUID(str(users[0].get("id") or users[0].get("user_id")))


async def test_a_change_is_recorded_exactly_when_history_shows_it(owner_ui, session, monkeypatch):
    """Class guard over the census: each field, set from each value it takes to each other
    one (and each field a writer drops, dropped from each), records an event exactly when
    its History line differs from that of no change, and an event recorded always reads as
    a line. A companion records exactly when its value changes."""
    from celerp.models.ledger import LedgerEntry
    from celerp.models.projections import Projection
    from celerp_subscriptions import billing
    from celerp_subscriptions.periods import UNSET, shown
    from test_history_and_lists import _changed_fields
    from ui.components.activity import detail_from_entry

    eid = await _changed_agreement(owner_ui, monkeypatch)
    cid = uuid.UUID(await company_id(owner_ui.api, {}))
    actor = await _owner_id(owner_ui)
    changes = await _changed_fields(owner_ui, eid)
    row = await session.get(Projection, {"company_id": cid, "entity_id": eid})
    base = dict(row.state)

    async def count() -> int:
        return (await session.execute(select(func.count()).select_from(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.entity_id == eid))).scalar_one()

    async def latest() -> LedgerEntry:
        return (await session.execute(select(LedgerEntry).where(
            LedgerEntry.company_id == cid, LedgerEntry.entity_id == eid)
            .order_by(LedgerEntry.id.desc()).limit(1))).scalar_one()

    async def write(f: str, held, change: dict, drop: tuple = ()) -> str | None:
        row = await session.get(Projection, {"company_id": cid, "entity_id": eid})
        row.state = {k: v for k, v in base.items() if k != f} | ({} if held is None else {f: held})
        await session.flush()
        before = await count()
        await billing.write_agreement(session, cid, eid, change, actor, drop=drop)
        if await count() == before:
            return None
        entry = await latest()
        return detail_from_entry(entry.event_type, entry.data) or ""

    rows = history_census.fields()
    sets = {f for w in history_census.writes() if w.through_sink for f in w.sets} - set(history_census.DERIVED)
    drops = {f for w in history_census.writes() if w.through_sink for f in w.drops}
    wrong = []
    for f in sorted(sets | drops):
        describe = _describer(f)
        values = _values(changes, base, f, UNSET.get(f))
        companion_set, companion_drop = f in history_census.COMPANION_SETS, f in history_census.COMPANION_DROPS
        for held in values:
            if f in sets:
                for new in values:
                    if new is None:
                        continue
                    if companion_set:
                        expected = shown(f, held) != shown(f, new)
                    else:
                        line = describe({"old": held, "new": new})
                        expected = line != describe({"old": new, "new": new})
                        if not expected and line:
                            wrong.append((f, held, new, "History shows a line for no change", line))
                    recorded = await write(f, held, {f: new})
                    if (recorded is not None) != expected:
                        wrong.append((f, held, new, "recorded" if recorded is not None else "not recorded", recorded))
                    elif recorded == "" and not companion_set:
                        wrong.append((f, held, new, "recorded a row that says nothing", recorded))
            if f in drops and held is not None:
                if companion_drop:
                    expected = shown(f, held) != shown(f, None)
                else:
                    expected = describe({"old": held, "drop": True}) != describe({"old": None, "drop": True})
                recorded = await write(f, held, {}, drop=(f,))
                if (recorded is not None) != expected:
                    wrong.append((f, held, "drop", "recorded" if recorded is not None else "not recorded", recorded))
    assert rows and not wrong, wrong


async def test_saving_what_the_page_shows_on_an_agreement_created_without_its_settings_records_nothing(
        owner_ui, monkeypatch):
    """An agreement made through Documents stores none of its billing settings; saving each
    control on its page with the value it shows adds no event and no History row."""
    from test_history_and_lists import _history

    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    stored = (await owner_ui.api.get(f"/docs/{eid}")).json()
    assert not {"generation_mode", "auto_send", "catchup_periods", "billing_timing"} & set(stored)
    soup = BeautifulSoup((await owner_ui.get(f"/subscriptions/{eid}")).text, "html.parser")
    shows = {}
    for el in soup.select("[hx-patch*='/schedule/']"):
        if el.name == "select":
            option = el.select_one("option[selected]") or el.select_one("option")
            shows[el["hx-patch"].rsplit("/", 1)[1]] = option.get("value", "")
        else:
            shows[el["hx-patch"].rsplit("/", 1)[1]] = el.get("value", "")
    assert {"generation_mode", "auto_send", "catchup_periods", "billing_timing", "frequency"} <= set(shows)
    ledger = len((await owner_ui.api.get("/ledger", params={"entity_id": eid, "limit": 200})).json()["items"])
    history = await _history(owner_ui, eid)
    for field, value in shows.items():
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/{field}", data={"value": value})
        assert r.status_code == 200, (field, value, r.text[:300])
    after = (await owner_ui.api.get("/ledger", params={"entity_id": eid, "limit": 200})).json()["items"]
    assert len(after) == ledger, [e["data"] for e in after[:len(after) - ledger]]
    assert await _history(owner_ui, eid) == history


async def test_a_real_change_names_only_what_changed(owner_ui, monkeypatch):
    """Switching an agreement made without its settings to automatic billing, with the
    catch-up window the page shows, names the mode and the consent and not the window."""
    from test_history_and_lists import _history
    from ui.i18n import t

    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-03-01")
    r = await owner_ui.api.patch(f"/subscriptions/{eid}/schedule",
                                 json={"generation_mode": "auto_invoice", "catchup_periods": 0})
    assert r.status_code == 200, r.text
    text = await _history(owner_ui, eid)
    assert t("subscriptions.mode_auto_invoice", "en") in text
    assert t("subscriptions.catchup_window", "en") not in text
