# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""An item's status and its default unit read in the user's language, on the item page and
in the inventory list."""

from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from celerp_inventory.routes import ITEM_STATUSES
from ui import i18n
from ui.components import table


@pytest.fixture
def german():
    i18n.set_lang("de")
    yield
    i18n.set_lang("en")


@pytest.mark.parametrize("status", sorted(ITEM_STATUSES))
def test_every_item_status_has_a_name_in_every_language(status):
    for lang in i18n.available_langs():
        key = f"enum.item_status.{status}"
        assert i18n.t(key, lang) != key, (lang, status)


def test_a_draft_item_reads_as_a_draft_in_german(german):
    assert table.display_enum("draft", "item_status") == "Entwurf"
    assert table.display_enum("available", "item_status") == "Verfügbar"


def test_the_default_unit_reads_in_german(german):
    assert table.display_unit("piece") == "Stück"
    assert table.display_unit("gram") == "Gramm"


def test_a_unit_the_company_named_shows_as_named(german):
    assert table.display_unit("tola") == "tola"
    assert table.display_unit("kg") == "kg"
    assert table.display_unit("", "Piece") == "Piece"


def test_the_quantity_and_purchase_cells_name_the_unit_in_german(german):
    qty = to_xml(table.paired_display_cell(entity_id="item:1", primary_field="quantity", primary_value=2,
                                     secondary_field="sell_by", secondary_value="piece"))
    purchase = to_xml(table.purchase_display_cell(entity_id="item:1", pu_val="piece", cf_val=1, sb_val="piece"))
    for xml in (qty, purchase):
        assert "Stück" in xml and ">piece<" not in xml, xml


@pytest.mark.parametrize("status,label", [("draft", "Entwurf"), ("available", "Verfügbar")])
def test_the_item_page_shows_the_status_in_german(german, status, label):
    from ui.routes.inventory import _detail_table
    xml = to_xml(_detail_table("item:1", {"status": status},
                               [{"key": "status", "label": "Status", "type": "status", "editable": False}]))
    assert label in xml, xml
