# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every overdue and awaiting-payment card links to the list filter that returns the
documents it counts: the dashboard receivables cards, the per-type status cards, and the All
documents cards. A status that no document has (``status=overdue``, ``status=outstanding``)
would open an empty list."""

from __future__ import annotations

import html as html_lib
import re

import pytest

_OVERDUE_HREF = "/docs?type=invoice&overdue_only=1"
_INVOICE_AWAITING = "awaiting_payment,final,partial,sent"
_BILL_AWAITING = "awaiting_payment,final,partial,partially_received,received"


def _dashboard_hrefs() -> list[tuple[str, str]]:
    from ui.routes.dashboard import _VERTICAL_CONFIGS

    out = []
    for vertical, cfg in _VERTICAL_CONFIGS.items():
        for card in cfg.get("kpis", []) + cfg.get("secondary_kpis", []):
            out.append((f"{vertical}:{card['key']}", card.get("href") or ""))
        for href, label, _desc in cfg.get("quick_links", []):
            out.append((f"{vertical}:{label}", href))
    return out


def test_dashboard_links_use_filters_that_match_documents():
    hrefs = _dashboard_hrefs()
    dead = [(k, h) for k, h in hrefs if re.search(r"[?&]status=(overdue|outstanding)\b", h)]
    assert not dead, dead
    from ui.routes.dashboard import _VERTICAL_CONFIGS
    for vertical, cfg in _VERTICAL_CONFIGS.items():
        for card in cfg.get("kpis", []) + cfg.get("secondary_kpis", []):
            if card["key"] == "ar_overdue":
                assert card["href"] == _OVERDUE_HREF, (vertical, card["href"])
            if card["key"] == "ar_outstanding":
                assert card["href"] == f"/docs?type=invoice&status_in={_INVOICE_AWAITING}", (vertical, card["href"])


def _card_hrefs(html: str) -> dict[str, str]:
    return {label: html_lib.unescape(href) for href, label in re.findall(
        r'<a href="([^"]*)" class="status-card[^"]*"><span class="status-card-label">([^<]*)</span>', html)}


@pytest.mark.parametrize("doc_type, awaiting", [("invoice", _INVOICE_AWAITING), ("bill", _BILL_AWAITING)])
def test_awaiting_card_links_to_the_type_awaiting_statuses(doc_type, awaiting):
    from ui.routes.documents import _doc_status_cards

    html = str(_doc_status_cards([], "", {"count_by_status": {}}, "USD", doc_type=doc_type, lang="en"))
    assert _card_hrefs(html)["Awaiting Payment"] == f"/docs?type={doc_type}&status_in={awaiting}"


@pytest.mark.parametrize("doc_type", ["", "quotation", "proforma", "receipt"])
def test_all_documents_overdue_card_counts_and_links_the_overdue_list(doc_type):
    """The generic cards read the overdue count from the summary (no document has the status
    "overdue") and link to the overdue filter."""
    from ui.routes.documents import _doc_status_cards

    summary = {"count_by_status": {"final": 4, "draft": 1}, "overdue_count": 3}
    html = str(_doc_status_cards([], "", summary, "USD", doc_type=doc_type, lang="en"))
    href = _card_hrefs(html)["Overdue"]
    assert "overdue_only=1" in href and "status=overdue" not in href, href
    count = re.search(r'overdue_only=1" class="status-card[^"]*">.*?<span class="status-card-count">(\d+)</span>', html)
    assert count and count.group(1) == "3", html


def test_all_documents_overdue_card_is_active_on_the_overdue_list():
    from ui.routes.documents import _doc_status_cards

    html = str(_doc_status_cards([], "", {"count_by_status": {}, "overdue_count": 2}, "USD", doc_type="", lang="en", overdue_only=True))
    assert re.search(r'overdue_only=1" class="status-card status-card--red status-card--active"', html), html


def test_overdue_statuses_are_awaiting_payment_statuses_for_money_types():
    """For an invoice or bill, overdue is a subset of awaiting payment."""
    from celerp.services.doc_balance import AWAITING_PAYMENT_STATUSES, OVERDUE_STATUSES

    assert set(AWAITING_PAYMENT_STATUSES) == {"invoice", "proforma", "memo", "bill", "purchase_order"}
    for doc_type in ("invoice", "bill"):
        assert OVERDUE_STATUSES[doc_type] <= AWAITING_PAYMENT_STATUSES[doc_type], doc_type
    assert AWAITING_PAYMENT_STATUSES["invoice"] == set(_INVOICE_AWAITING.split(","))
    assert AWAITING_PAYMENT_STATUSES["bill"] == set(_BILL_AWAITING.split(","))


@pytest.mark.parametrize("state, owed", [
    ({"total": 50}, 50),
    ({"total": 50, "amount_outstanding": None}, 50),
    ({"total": 50, "amount_outstanding": 0}, 0),
    ({"total": 50, "amount_outstanding": 0.0}, 0),
    ({"total": 50, "amount_outstanding": 12.5}, 12.5),
    ({"total_amount": 50}, 50),
    ({"total_amount": 50, "outstanding_balance": 0}, 0),
    ({"total_amount": 50, "outstanding_balance": 7}, 7),
    ({"total": 50, "amount_outstanding": ""}, 50),
    ({}, 0),
])
def test_outstanding_balance(state, owed):
    from celerp.services.doc_balance import outstanding_balance

    assert outstanding_balance(state) == pytest.approx(owed)


def test_outstanding_balance_of_a_non_number_is_unknown():
    from celerp.services.doc_balance import outstanding_balance

    assert outstanding_balance({"total": 50, "amount_outstanding": "N/A"}) is None
