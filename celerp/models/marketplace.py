# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1
# Re-export from owning module (celerp-connectors).
# This file is kept for backward compat with Alembic env.py import.
try:
    from celerp_connectors.models import MarketplaceConfig  # noqa: F401
except ImportError:
    pass
