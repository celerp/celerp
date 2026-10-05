# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Fixed UI labels read in the user's language; user data stays as entered.

The three system price lists every company starts with (Retail, Wholesale, Cost)
cannot be deleted and are UI labels: the inventory totals chips and the price
column headers translate them. A price list the user created is data and shows
as typed. Every sidebar entry a first-party module contributes carries a
translation key (German showed "Demand Planning", "Work In Progress" and
"Stock Orders" in English)."""

from __future__ import annotations

import ast
import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from celerp.services.field_schema import _inject_price_columns
from celerp.services.pricing import PRICE_LISTS_FALLBACK
from ui.i18n import _current_lang, field_label, t

_ROOT = Path(__file__).resolve().parents[1]


def _in_lang(lang, fn):
    token = _current_lang.set(lang)
    try:
        return fn()
    finally:
        _current_lang.reset(token)


def test_system_price_columns_translate_and_user_lists_do_not():
    cols = _inject_price_columns([], PRICE_LISTS_FALLBACK + [{"name": "VIP"}])
    labels = _in_lang("de", lambda: {f["key"]: field_label(f) for f in cols})
    assert labels["retail_price"] == "Einzelhandel"
    assert labels["wholesale_price"] == "Großhandel"
    assert labels["cost_price"] == "Kosten"
    assert labels["cost_price_total"] == "Kosten (Gesamt)"
    assert labels["vip_price"] == "VIP"
    en = _in_lang("en", lambda: {f["key"]: field_label(f) for f in cols})
    assert en["retail_price"] == "Retail" and en["cost_price_total"] == "Cost (Total)"


def test_renamed_price_column_label_is_user_data():
    # A stored schema whose cost column label the user changed keeps the typed label.
    f = {"key": "cost_price", "label": "Einstandspreis"}
    assert _in_lang("de", lambda: field_label(f)) == "Einstandspreis"


def test_valuation_chips_translate_system_price_lists():
    from ui.routes.inventory import _valuation_bar
    bar = _in_lang("de", lambda: _valuation_bar(
        {"item_count": 3, "price_totals": {"Retail": 10, "Cost": 4, "VIP": 7}}, "USD", "de"))
    html = str(bar)
    assert "Einzelhandel:" in html and "Kosten:" in html and "VIP:" in html
    assert "Retail:" not in html and "Cost:" not in html


def _module_nav_entries():
    for init in sorted(_ROOT.glob("default_modules/*/__init__.py")):
        for node in ast.walk(ast.parse(init.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            d = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            if {"group", "href", "label"} <= d.keys():
                yield init.parent.name, d


def test_every_module_sidebar_entry_is_keyed():
    import json
    de = json.loads((_ROOT / "ui" / "locales" / "de.json").read_text(encoding="utf-8"))
    gaps = []
    for module, d in _module_nav_entries():
        key = d["label_key"].value if "label_key" in d else None
        if key is None or key not in de:
            gaps.append(f"{module}: {d['label'].value}")
    assert not gaps, gaps


def test_manufacturing_sidebar_reads_german():
    labels = {d["href"].value: t(d["label_key"].value, "de") if "label_key" in d else d["label"].value
              for module, d in _module_nav_entries() if module == "celerp-manufacturing"}
    assert labels == {"/manufacturing": "Bedarfsplanung", "/manufacturing/production": "Ware in Arbeit",
                      "/docs?type=production_order": t("documents.page_stock_orders", "de")}
    assert "Stock Orders" not in labels.values()
