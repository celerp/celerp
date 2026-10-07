# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Contact and deal events in the activity feed carry keyed labels from the same
table as every other event, in every catalog: never the title-cased raw type
("Crm Contact Created"). An empty DETAILS cell is always "--"."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from celerp.events.schemas import EVENT_SCHEMA_MAP
from ui import i18n
from ui.components.activity import EVENT_TYPE_LABELS, activity_table, event_label

_LOCALES = Path(__file__).parent.parent / "ui" / "locales"
_CATALOGS = {p.stem: json.loads(p.read_text()) for p in sorted(_LOCALES.glob("*.json"))}

# Contact and deal events, file events excluded (they share the file-op labels).
_CRM_TYPES = sorted(et for et in EVENT_SCHEMA_MAP
                    if et.startswith("crm.") and ".file" not in et)


@pytest.fixture(autouse=True)
def _reset_lang():
    yield
    i18n.set_lang("en")


@pytest.mark.parametrize("event_type", _CRM_TYPES)
def test_crm_event_has_a_keyed_label_in_every_catalog(event_type):
    key = EVENT_TYPE_LABELS.get(event_type)
    assert key, f"{event_type} has no label key"
    for lang, cat in _CATALOGS.items():
        assert cat.get(key), (lang, key)
    i18n.set_lang("de")
    assert event_label(event_type) == _CATALOGS["de"][key] != _CATALOGS["en"][key]
    assert "Crm" not in event_label(event_type)


def test_contact_created_reads_contact_created():
    assert event_label("crm.contact.created") == "Contact created"


def test_label_table_names_only_types_the_ledger_writes():
    """A label for a type nothing emits ("contact.created") is a label the feed never
    uses while the real type falls back to its raw name."""
    for dead in ("contact.created", "contact.updated", "deal.created", "deal.updated",
                 "deal.won", "deal.lost"):
        assert dead not in EVENT_TYPE_LABELS, dead
        assert f"event.{dead}" not in _CATALOGS["en"], dead


@pytest.mark.parametrize("lang", ["en", "de"])
def test_empty_details_cell_is_always_two_dashes(lang):
    i18n.set_lang(lang)
    ledger = [
        {"id": 1, "event_type": "crm.contact.created", "entity_id": "contact:c1",
         "ts": "2026-10-01T10:00:00Z", "data": {"name": "Acme"}},
        {"id": 2, "event_type": "doc.finalized", "entity_id": "doc:d1",
         "ts": "2026-10-01T09:00:00Z", "data": {}},
        {"id": 3, "event_type": "item.created", "entity_id": "item:i1",
         "ts": "2026-10-01T08:00:00Z", "data": {"name": "Ring"}},
    ]
    html = to_xml(activity_table(ledger))
    assert "Crm Contact Created" not in html
    assert _CATALOGS[lang]["event.crm.contact.created"] in html
    for rid in (1, 2, 3):
        row = re.search(rf'<tr id="evt-{rid}">(.*?)</tr>', html, re.S)
        assert row, rid
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S)
        assert cells[-1].strip() == "--", (rid, cells[-1])
