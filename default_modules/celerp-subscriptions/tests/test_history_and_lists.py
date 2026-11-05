# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""What the agreement's History says about each change, how counts read in the
subscription pages, and the tables that bill several agreements at once."""
from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from agreement_kit import agreement, company_id, freeze_day, generate_due, schedule, tick
from ui.i18n import sentence, t

REPO = Path(__file__).resolve().parents[3]


async def _history(ui, eid: str) -> str:
    return BeautifulSoup((await ui.get(f"/docs/{eid}/history?per_page=100")).text, "html.parser").get_text(" ", strip=True)


async def _changed_fields(ui, eid: str) -> list[dict]:
    """Each ``fields_changed`` the agreement's ledger holds, oldest first."""
    r = await ui.api.get("/ledger", params={"entity_id": eid, "limit": 200})
    assert r.status_code == 200, r.text
    items = r.json()["items"] if isinstance(r.json(), dict) else r.json()
    return [e["data"]["fields_changed"] for e in reversed(items)
            if isinstance(e.get("data"), dict) and e["data"].get("fields_changed")]


async def _changed_agreement(ui, monkeypatch) -> str:
    """An agreement with each kind of change its page and API make: every schedule term,
    both automatic modes and their consent, a bill, a skip and its undo."""
    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(ui.api, {}, start="2026-01-01")
    for field, value in (("billing_timing", "arrears"), ("billing_timing", "advance"), ("term_end", "2026-12-31"),
                         ("cancel_at_period_end", "true"), ("cancel_at_period_end", "false"),
                         ("generation_mode", "auto_invoice"), ("auto_send", "true"), ("catchup_periods", "2"),
                         ("auto_send", "false"), ("generation_mode", "manual_due")):
        r = await ui.patch(f"/subscriptions/{eid}/schedule/{field}", data={"value": value})
        assert r.status_code == 200, (field, r.text[:300])
    assert (await generate_due(ui.api, {}, eid)).status_code == 200
    assert (await ui.api.post(f"/subscriptions/{eid}/periods/1/skip", json={"reason": "Closed for the holiday"})).status_code == 200
    assert (await ui.api.post(f"/subscriptions/{eid}/periods/1/unskip")).status_code == 200
    return eid


# --- History (P4-11) ---------------------------------------------------------

async def test_history_names_each_change_as_the_agreement_page_does(owner_ui, monkeypatch):
    eid = await _changed_agreement(owner_ui, monkeypatch)
    text = await _history(owner_ui, eid)
    for raw in ("Generation Mode", "Auto Send", "Catchup Periods", "Consents", "Periods updated", "Billing Timing",
                "Cancel At Period End", "auto_invoice", "manual_due", "True", "False", "→ arrears", "→ advance"):
        assert raw not in text, raw
    timing = (t("subscriptions.timing_advance", "en"), t("subscriptions.timing_arrears", "en"))
    assert f"{t('subscriptions.billing_timing', 'en')}: {timing[0]} → {timing[1]}" in text
    mode = t("subscriptions.billing_mode", "en")
    assert f"{mode}: {t('subscriptions.mode_manual_due', 'en')} → {t('subscriptions.mode_auto_invoice', 'en')}" in text
    assert f"{t('subscriptions.auto_send', 'en')}: {t('settings.no', 'en')} → {t('settings.yes', 'en')}" in text
    assert f"{t('subscriptions.stop_at_period_end', 'en')}: {t('settings.no', 'en')} → {t('settings.yes', 'en')}" in text
    assert f"{t('subscriptions.catchup_window', 'en')}: 0 → 2" in text
    assert t("subscriptions.consent_generation", "en", date="").strip() in text
    assert t("subscriptions.consent_generation_withdrawn", "en") in text


async def test_history_names_the_period_billed_skipped_and_opened_again(owner_ui, monkeypatch):
    eid = await _changed_agreement(owner_ui, monkeypatch)
    text = await _history(owner_ui, eid)
    assert sentence("subscriptions.history_billed", "en", start="2026-01-01", end="2026-01-31") in text
    assert sentence("subscriptions.history_skipped", "en", start="2026-02-01", end="2026-02-28",
             reason="Closed for the holiday") in text
    assert sentence("subscriptions.done_unskipped", "en", start="2026-02-01", end="2026-02-28") in text


async def test_every_field_an_agreement_records_reads_by_its_label(owner_ui, monkeypatch):
    """Class guard: no field an agreement's changes record falls back to its internal
    name, in English or German."""
    from ui import i18n
    from ui.components.activity import detail_from_entry

    eid = await _changed_agreement(owner_ui, monkeypatch)
    changes = await _changed_fields(owner_ui, eid)
    seen = {k for c in changes for k in c}
    assert {"generation_mode", "auto_send", "consents", "periods", "catchup_periods"} <= seen
    try:
        for lang in ("en", "de"):
            i18n.set_lang(lang)
            for change in changes:
                for k, v in change.items():
                    raw = k.replace("_", " ").title()
                    if raw == t(f"field.{k}", lang):
                        continue  # a core field whose label reads as its name (Status)
                    detail = detail_from_entry({"fields_changed": {k: v}}, "doc.updated")
                    assert raw not in detail, (lang, k, detail)
    finally:
        i18n.set_lang("en")


async def test_history_field_labels_follow_the_reader_language(owner_ui, monkeypatch):
    eid = await _changed_agreement(owner_ui, monkeypatch)
    owner_ui.cookies.set("celerp_lang", "de")
    text = await _history(owner_ui, eid)
    assert t("subscriptions.billing_mode", "de") in text and t("subscriptions.auto_send", "de") in text
    assert "Auto Send" not in text and "Generation Mode" not in text


# --- Counts (P4-12) ----------------------------------------------------------

def test_no_subscription_message_puts_a_count_before_a_plural_noun():
    """Class guard: "1 agreements" cannot be written. A count reads after its label
    ("Agreements: 1"), or the message has a ``_one`` form for one."""
    catalog = json.loads((REPO / "ui/locales/en.json").read_text(encoding="utf-8"))
    bad = {k: v for k, v in catalog.items() if k.startswith("subscriptions.") and not k.endswith("_one")
           and re.search(r"\{(n|m|count)\} [a-z]+s\b", v) and f"{k}_one" not in catalog}
    assert not bad, bad


async def test_one_held_agreement_reads_as_one(owner_ui, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    r = await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice", catchup_periods=0)
    assert r.status_code == 200, r.text
    freeze_day(monkeypatch, "2026-02-15")  # two periods due, a window of none: both held
    await tick(session, await company_id(owner_ui.api, {}), "2026-02-15")
    for lang in ("en", "de"):
        owner_ui.cookies.set("celerp_lang", lang)
        page = (await owner_ui.get("/subscriptions?direction=sales")).text
        banner = t("subscriptions.catchup_banner", lang, n=1, m=2)
        assert banner in page, (lang, page[:500])
        assert not re.search(r"\b1 (agreements|periods|Vereinbarungen|Zeiträume)\b", banner), banner


# --- Tables that bill several agreements (P4-14) -----------------------------

def _table_checks(html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    table = next(tb for tb in soup.select("table") if tb.select("tbody input[type=checkbox]"))
    heads = table.select("thead th")
    for th in heads:
        box = th.select_one("input[type=checkbox]")
        assert th.get_text(strip=True) or (box and box.get("title")), str(th)
    select_all = table.select_one("thead input.bulk-select-all")
    assert select_all is not None and select_all.get("title") == t("label.select_all", "en")
    rows = table.select("tbody tr")
    assert rows and all(r.select_one("input.bulk-select") for r in rows)
    assert all("data-row" in (r.get("class") or []) for r in rows)
    assert soup.find("script", string=re.compile("bulk-select-all")) is not None


async def test_generate_all_due_has_select_all_and_named_columns(owner_ui, monkeypatch):
    freeze_day(monkeypatch, "2026-03-15")
    await agreement(owner_ui.api, {}, start="2026-01-01")
    html = (await owner_ui.get("/subscriptions/due?direction=sales")).text
    _table_checks(html)
    assert t("subscriptions.th_more_due", "en") in html
    assert not re.search(r"\b1 agreements\b", BeautifulSoup(html, "html.parser").get_text(" "))


async def test_catch_up_table_has_select_all_and_named_columns(owner_ui, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice", catchup_periods=0)).status_code == 200
    freeze_day(monkeypatch, "2026-03-15")
    await tick(session, await company_id(owner_ui.api, {}), "2026-03-15")
    _table_checks((await owner_ui.get("/subscriptions/catch-up")).text)


def test_every_select_all_box_is_the_shared_one():
    """Class guard: a table's select-all header comes from ``select_all_th``, which names
    it and brings the script that ticks the rows."""
    hits = []
    for path in [*REPO.glob("ui/**/*.py"), *REPO.glob("default_modules/*/celerp_*/**/*.py")]:
        if path.name == "table.py" and path.parent.name == "components":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "bulk-select-all" in line:
                hits.append(f"{path.relative_to(REPO)}:{n}")
    assert not hits, hits


# --- No History row for a change that changed nothing (P4b-10) -------------------

async def _agreement_events(ui, eid: str) -> list[dict]:
    """Every doc.updated the agreement's ledger holds, with its fields_changed."""
    r = await ui.api.get("/ledger", params={"entity_id": eid, "limit": 500})
    assert r.status_code == 200, r.text
    items = r.json()["items"] if isinstance(r.json(), dict) else r.json()
    return [e for e in items if e.get("event_type") == "doc.updated"]


def _said_nothing(event: dict) -> bool:
    fields = (event.get("data") or {}).get("fields_changed") or {}
    return not fields or all(isinstance(v, dict) and not v.get("drop") and v.get("old") == v.get("new")
                             for v in fields.values())


async def test_saving_a_setting_with_its_current_value_records_nothing(owner_ui, monkeypatch):
    """Class guard: every schedule and billing setting the page saves, saved again with the
    value it holds, leaves no ledger event and no History row."""
    from celerp_subscriptions.routes import ScheduleBody

    freeze_day(monkeypatch, "2026-03-15")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    assert (await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice", auto_send=True,
                           catchup_periods=2, term_end="2026-12-31", term_start="2026-01-01")).status_code == 200
    state = (await owner_ui.api.get(f"/docs/{eid}")).json()
    before = len(await _agreement_events(owner_ui, eid))
    for field in ScheduleBody.model_fields:
        value = state.get(field)
        if field == "cancel_at_period_end":
            value = value is not None  # the page's Yes/No; the stored period is kept
        text = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        r = await owner_ui.patch(f"/subscriptions/{eid}/schedule/{field}", data={"value": text})
        assert r.status_code == 200, (field, r.text[:300])
    events = await _agreement_events(owner_ui, eid)
    assert len(events) == before, [e["data"] for e in events[:len(events) - before]]


async def test_every_bill_path_records_only_what_changed(owner_ui, session, monkeypatch):
    """Class guard: Generate due, Bill early, Generate all due and the catch-up run each
    record the bill and nothing that reads as an empty History row."""
    freeze_day(monkeypatch, "2026-03-15")
    manual = await agreement(owner_ui.api, {}, start="2026-03-01")
    assert (await generate_due(owner_ui.api, {}, manual)).status_code == 200
    early = await owner_ui.api.post(f"/subscriptions/{manual}/bill-early", json={"request_key": "early-1"})
    assert early.status_code == 200, early.text
    bulk = await agreement(owner_ui.api, {}, start="2026-03-01")
    r = await owner_ui.api.post("/subscriptions/generate-due",
                                json={"items": [{"entity_id": bulk, "period_index": 0, "request_key": "all-1"}]})
    assert r.json()["billed"] == 1, r.text
    freeze_day(monkeypatch, "2026-01-01")
    held = await agreement(owner_ui.api, {}, start="2026-01-01")
    assert (await schedule(owner_ui.api, {}, held, generation_mode="auto_invoice")).status_code == 200
    freeze_day(monkeypatch, "2026-03-15")
    r = await owner_ui.api.post("/subscriptions/catch-up/run",
                                json={"items": [{"entity_id": held, "period_index": 0, "request_key": "cu-1"}]})
    assert r.json()["results"][0]["ok"], r.text
    for eid in (manual, bulk, held):
        events = await _agreement_events(owner_ui, eid)
        assert not [e for e in events if _said_nothing(e)], (eid, [e["data"] for e in events if _said_nothing(e)])
        assert "--" not in (await _history(owner_ui, eid)).split(), eid
    # The receipt still answers a repeated request with the first answer.
    again = await owner_ui.api.post(f"/subscriptions/{manual}/bill-early", json={"request_key": "early-1"})
    assert again.status_code == 200 and again.json() == early.json(), again.text


def test_only_the_one_writer_records_an_agreement_change():
    """Class guard: inside celerp_subscriptions an agreement's doc.updated event is written
    only by billing.write_agreement (which drops what did not change)."""
    import ast

    allowed = {("billing.py", "write_agreement")}
    found = set()
    for path in (REPO / "default_modules/celerp-subscriptions/celerp_subscriptions").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "emit_event"
                        and any(k.arg == "event_type" and getattr(k.value, "value", None) == "doc.updated"
                                for k in node.keywords)):
                    found.add((path.name, fn.name))
    assert found == allowed, found
