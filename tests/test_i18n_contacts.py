# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Render-time language resolution for the contacts routes.

The contacts page builds every user-facing string by calling ``t()`` at render
time: the contact table schema stores translation KEYS and resolves them in
``_contact_schema`` (never at import), financial summary cards call ``t()`` when
built, the bulk-merge template hands translated text to its JavaScript through
``data-*`` attributes, and count-bearing strings interpolate neutrally. These
tests register a sentinel language ``xx`` and assert its text reaches the
rendered output while ``xx`` is active. They are red against a tree that
resolves any of these strings at import time or hardcodes English.
"""

import pytest

from fasthtml.common import to_xml

from ui import i18n
from ui.routes.contacts import (
    _contact_schema,
    _contacts_content,
    _financial_summary,
    _contact_bulk_templates,
)

# Sentinel catalog: one unmistakable value per mechanism the contacts page uses.
_XX = {
    # module-dict schema label (resolved at render in _contact_schema)
    "contacts.field_website": "XX_WEBSITE",
    # table-header label (schema label rendered as a <th> by data_table)
    "contacts.field_billing_address": "XX_BILLING",
    # direct t() label inside a rendered component (financial summary card)
    "contacts.total_invoiced": "XX_TOTAL_INVOICED",
    "contacts.year_to_date": "XX_YTD",
    # JS data-attr value (handed to script, never spliced into JS source)
    "contacts.merge_failed": "XX_MERGE_FAILED",
    # count-neutral interpolation
    "contacts.merge_prompt": "XX_MERGE",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    """Register the sentinel language, make it active, and reset both the registry
    and the context language afterwards so nothing leaks between tests."""
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def test_schema_label_resolves_at_render():
    """Module-dict mechanism: _CONTACT_SCHEMA_BASE stores keys; _contact_schema
    resolves them in the active language when called."""
    schema = _contact_schema("customer")
    website = next(f for f in schema if f["key"] == "website")
    assert website["label"] == "XX_WEBSITE"


def test_table_header_translates():
    """Table-header mechanism: the resolved schema label is rendered as a <th>
    by data_table inside the list content."""
    html = to_xml(_contacts_content(
        "customer",
        [{"name": "Acme Co", "entity_id": "contact:1"}],
        "", 1, 1, 50, "created_at", "desc",
    ))
    assert "XX_BILLING" in html


def test_financial_card_label_translates():
    """Component t() mechanism: financial summary cards call t() at build time."""
    html = to_xml(_financial_summary([], contact_id="contact:1"))
    assert "XX_TOTAL_INVOICED" in html
    assert "XX_YTD" in html


def test_bulk_merge_template_data_attr_translates():
    """JS data-attr mechanism: the merge template exposes the failure message to
    its script via data-merge-failed rather than splicing it into JS source."""
    html = to_xml(_contact_bulk_templates())
    assert "XX_MERGE_FAILED" in html


def test_merge_prompt_translates():
    html = to_xml(_contact_bulk_templates())
    assert "XX_MERGE" in html


@pytest.mark.parametrize("lang", ["th", "de"])
@pytest.mark.parametrize("contact_type", ["customer", "vendor"])
def test_an_empty_list_names_its_contact_type_in_the_reader_language(lang, contact_type):
    """The empty list's sentence and button name the contact type by its label in the
    reader's language, never the raw value."""
    i18n.set_lang(lang)
    label = i18n.t(f"enum.contact_type.{contact_type}", lang)
    html = to_xml(_contacts_content(contact_type, [], "", 1, 0, 25, "name", "asc"))
    assert i18n.t("contacts.empty_none", lang, type=label) in html
    assert i18n.t("contacts.add_type", lang, type=label) in html
