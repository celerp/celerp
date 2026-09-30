# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Celerp's own synthetic Manager business file, offered as a sample migration source.

Every name, amount and document in it is invented. It is written by the same
independent encoder as the Manager test fixtures and contains no vendor data.
"""

from pathlib import Path

SAMPLE_ARTIFACT: Path = Path(__file__).resolve().parent / "sample.manager"
SAMPLE_COMPANY_NAME = "Sample company"
