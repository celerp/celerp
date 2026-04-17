# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Import-coverage tests for modules that are hard to exercise via integration tests."""

from celerp.events.types import EventType
from celerp.models.marketplace import MarketplaceConfig


def test_event_type_values() -> None:
    assert EventType.ITEM_CREATED == "item.created"
    assert EventType.CRM_CONTACT_CREATED == "crm.contact.created"
    assert EventType.MFG_ORDER_CREATED == "mfg.order.created"
    assert EventType.DOC_CREATED == "doc.created"
    assert EventType.SCAN_BARCODE == "scan.barcode"
    assert EventType.MP_LISTING_CREATED == "mp.listing.created"
    assert EventType.ACC_JOURNAL_ENTRY_CREATED == "acc.journal_entry.created"
    assert EventType.SYS_COMPANY_CREATED == "sys.company.created"
    # Spot-check total count
    assert len(EventType) == 89


def test_marketplace_config_model_is_importable() -> None:
    assert MarketplaceConfig.__tablename__ == "marketplace_configs"
