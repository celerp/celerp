# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Import-coverage tests for modules that are hard to exercise via integration tests."""

import os

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
    assert len(EventType) == 101


def test_log_level_default_is_info() -> None:
    """Settings.log_level defaults to INFO when LOG_LEVEL env var is unset."""
    # Ensure env var is not set for this test
    env_val = os.environ.pop("LOG_LEVEL", None)
    try:
        # Re-instantiate to pick up clean env
        from pydantic_settings import BaseSettings
        from celerp.config import Settings
        s = Settings()
        assert s.log_level.upper() == "INFO"
    finally:
        if env_val is not None:
            os.environ["LOG_LEVEL"] = env_val


def test_log_level_env_override() -> None:
    """LOG_LEVEL env var is picked up by Settings."""
    os.environ["LOG_LEVEL"] = "debug"
    try:
        from celerp.config import Settings
        s = Settings()
        assert s.log_level.lower() == "debug"
    finally:
        del os.environ["LOG_LEVEL"]



def test_marketplace_config_model_is_importable() -> None:
    assert MarketplaceConfig.__tablename__ == "marketplace_configs"
