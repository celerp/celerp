# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the Accounting settings routes.

The chart-of-accounts, bank-account, reconciliation-rule and period-lock views
build every label, heading, button, placeholder, help line and enum display
label by calling ``t()`` (directly or via ``display_enum``) at render time. These
tests register a sentinel language ``xx`` and assert its text reaches the rendered
output while ``xx`` is active. They are red against a tree that hardcodes the
English strings or resolves them at import time.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.components.shell import page_title
from ui.routes.settings_accounting import (
    _accounting_settings_tabs,
    _bank_account_row,
    _chart_table,
    _rules_tab,
    _period_lock_tab,
    _account_form,
    _cash_flow_display_cell,
)

# Sentinel catalog: one unmistakable value per key these views render. Covers
# every conversion mechanism used in the file: plain t() (buttons, help, tabs,
# placeholders), interpolation, and display_enum enum labels (account_type,
# cash_flow, bank_type, match_type).
_XX = {
    "enum.account_type.asset": "XX_ASSET",
    "enum.account_type.expense": "XX_EXPENSE",
    "enum.cash_flow.financing": "XX_FINANCING",
    "enum.cash_flow.operating": "XX_OPERATING",
    "enum.bank_type.checking": "XX_CHECKING",
    "enum.match_type.starts_with": "XX_STARTSWITH",
    "th.active": "XX_ACTIVE",
    "settings.inactive": "XX_INACTIVE",
    "inv.archive": "XX_ARCHIVE",
    "btn.restore": "XX_RESTORE",
    "settings.click_to_edit": "XX_CLICKEDIT",
    "settings_accounting.tab_bank_accounts": "XX_TABBANK",
    "settings_accounting.chart_of_accounts": "XX_COA",
    "settings_accounting.finance_settings": "XX_FINANCE",
    "settings_accounting.period_lock_help": "XX_PLHELP",
    "settings_accounting.currently_locked_through": "XX_LOCKED {lock_date} {updated}",
    "settings_accounting.close_year_confirm": "XX_CONFIRM",
    "settings_accounting.delete_rule_confirm": "XX_DELRULE",
    "settings_accounting.ph_match_pattern": "XX_PHPATTERN",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_chart_table_enum_and_status_labels_translate():
    chart = [
        {"code": "1000", "name": "Cash", "account_type": "asset",
         "cash_flow_category": "financing", "is_active": True},
        {"code": "5000", "name": "COGS", "account_type": "expense", "is_active": False},
    ]
    html = to_xml(_chart_table(chart))
    # account_type: section header AND badge, both via display_enum
    assert "XX_ASSET" in html
    assert "XX_EXPENSE" in html
    # cash flow display cell via display_enum
    assert "XX_FINANCING" in html
    # active/inactive status labels
    assert "XX_ACTIVE" in html
    assert "XX_INACTIVE" in html
    # raw values stay canonical for the badge class
    assert "badge--asset" in html


def test_bank_account_row_translates_type_and_action():
    b = {"id": "b1", "bank_name": "KBank", "bank_type": "checking",
         "account_number": "111", "currency": "USD", "balance": 5.0, "is_active": True}
    html = to_xml(_bank_account_row(b))
    assert "XX_CHECKING" in html   # bank_type via display_enum
    assert "XX_ARCHIVE" in html    # active account offers Archive


def test_rules_tab_translates_match_type_confirm_and_placeholder():
    rules = [{"id": "r1", "match_pattern": "ACME", "match_type": "starts_with",
              "target_account_code": "6950", "is_active": True}]
    banks = [{"id": "b1", "bank_name": "KBank", "account_number": "111"}]
    html = to_xml(_rules_tab(rules, banks))
    assert "XX_STARTSWITH" in html   # match_type display_enum (table cell + select)
    assert "XX_DELRULE" in html      # delete confirm attribute
    assert "XX_PHPATTERN" in html    # search-pattern placeholder


def test_period_lock_tab_translates_help_and_interpolates_status():
    lock = {"lock_date": "2026-01-01", "lock_date_set_at": "2026-01-02T00:00:00"}
    html = to_xml(_period_lock_tab(lock))
    assert "XX_PLHELP" in html
    assert "XX_LOCKED" in html       # currently-locked message, interpolated
    assert "2026-01-01" in html      # the interpolated lock date
    assert "XX_CONFIRM" in html      # close-year confirm attribute


def test_account_form_translates_type_and_status_options():
    html = to_xml(_account_form([], values={
        "code": "1000", "name": "Cash", "account_type": "asset", "is_active": True}))
    assert "XX_ASSET" in html    # account_type option via display_enum
    assert "XX_ACTIVE" in html   # is_active option


def test_tabs_and_click_to_edit_translate():
    tabs_html = to_xml(_accounting_settings_tabs("chart"))
    assert "XX_TABBANK" in tabs_html
    assert "XX_COA" in tabs_html
    cell_html = to_xml(_cash_flow_display_cell("1000", "operating"))
    assert "XX_OPERATING" in cell_html   # display_enum cash flow label
    assert "XX_CLICKEDIT" in cell_html    # click-to-edit tooltip


def test_page_title_uses_translated_key():
    # R5: browser <title> composed from a translation key at render time
    assert page_title("settings_accounting.finance_settings") == "XX_FINANCE - Celerp"
