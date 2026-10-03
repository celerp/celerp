# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from celerp.services.business_time import business_date_at


def test_business_date_uses_business_timezone_and_rejects_ambiguity():
    assert business_date_at(
        datetime(2024, 1, 1, 17, 30, tzinfo=timezone.utc), "Asia/Bangkok"
    ) == "2024-01-02"
    assert business_date_at(
        datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), "America/Los_Angeles"
    ) == "2023-12-31"
    assert business_date_at(
        datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc), None
    ) == "2024-01-01"
    with pytest.raises(ValueError):
        business_date_at(datetime(2024, 1, 1, 2, 30), "UTC")
    with pytest.raises(ValueError):
        business_date_at(
            datetime(2024, 1, 1, 2, 30, tzinfo=timezone.utc),
            "Synthetic/Invalid",
        )

