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
from ui.i18n import t

REPO = Path(__file__).resolve().parents[3]


async def _history(ui, eid: str) -> str:
    return BeautifulSoup((await ui.get(f"/docs/{eid}/history")).text, "html.parser").get_text(" ", strip=True)


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
                "Cancel At Period End", "auto_invoice", "manual_due", "True", "False", "arrears"):
        assert raw not in text, raw
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
    assert t("subscriptions.history_billed", "en", start="2026-01-01", end="2026-01-31") in text
    assert t("subscriptions.history_skipped", "en", start="2026-02-01", end="2026-02-28",
             reason="Closed for the holiday") in text
    assert t("subscriptions.done_unskipped", "en", start="2026-02-01", end="2026-02-28") in text


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


async def test_one_held_period_reads_as_one(owner_ui, session, monkeypatch):
    freeze_day(monkeypatch, "2026-01-01")
    eid = await agreement(owner_ui.api, {}, start="2026-01-01")
    r = await schedule(owner_ui.api, {}, eid, generation_mode="auto_invoice", catchup_periods=0)
    assert r.status_code == 200, r.text
    freeze_day(monkeypatch, "2026-01-15")
    await tick(session, await company_id(owner_ui.api, {}), "2026-01-15")
    for lang in ("en", "de"):
        owner_ui.cookies.set("celerp_lang", lang)
        page = (await owner_ui.get("/subscriptions?direction=sales")).text
        banner = t("subscriptions.catchup_banner", lang, n=1, m=1)
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
