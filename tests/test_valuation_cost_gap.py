"""The valuation bar says an item missing from the Cost list has an unknown cost, and an item
missing from a sale list has no price."""
from fasthtml.common import to_xml

from ui.i18n import t
from ui.routes.inventory import _valuation_bar


def test_cost_gap_reads_unknown_cost_and_sale_gap_reads_without_a_price():
    html = to_xml(_valuation_bar({"item_count": 3, "price_totals": {"Cost": 207, "Retail": 900},
                                  "price_missing": {"Cost": 1, "Retail": 2}}, "USD"))
    assert t("inventory.cost_unknown_count", n=1) in html
    assert t("inventory.sold_without_price", n=2) in html
    assert t("inventory.sold_without_price", n=1) not in html
