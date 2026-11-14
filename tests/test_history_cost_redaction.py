# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The History renderer must not recover costs stripped for the reader's role."""
from datetime import datetime, timezone

from fasthtml.common import to_xml

from celerp.models.ledger import LedgerEntry
from celerp.services.activity_redaction import serialize_event
from ui.components.activity import history_rows, history_table


def test_cost_fields_stay_hidden_in_history():
    ts = datetime(2026, 11, 7, 3, tzinfo=timezone.utc)
    entries = [LedgerEntry(
        id=1, event_type="item.updated", entity_id="item:history-cost", entity_type="item", ts=ts,
        data={"fields_changed": {
            "cost_price": {"old": 98765.43, "new": 87654.32},
            "cost_total": {"old": 76543.21, "new": 65432.10},
            "name": {"old": "Before", "new": "After"},
        }}), LedgerEntry(
        id=2, event_type="item.pricing.set", entity_id="item:history-cost", entity_type="item", ts=ts,
        data={"price_type": "cost", "price": 54321.09, "old_price": 43210.98})]

    def read_as(role):
        return [{**serialize_event(e, {}, role), "actor_name": "Owner"} for e in entries]
    hidden = read_as("operator")
    rows = history_rows(hidden, currency="USD")
    html = to_xml(history_table(hidden, currency="USD", filter_url="/history"))
    assert "Before" in html and "After" in html
    assert all("cost" not in r["field"].lower() for r in rows)
    for amount in (98765.43, 87654.32, 76543.21, 65432.10, 54321.09, 43210.98):
        assert f"{amount:.2f}" not in html
        assert f"{amount:,.2f}" not in html
    visible = to_xml(history_table(read_as("manager"), currency="USD"))
    assert "98,765.43" in visible and "87,654.32" in visible
    assert entries[0].data["fields_changed"]["cost_price"]["old"] == 98765.43
