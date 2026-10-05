# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory list of setup's sample items, shared by the dashboard's demo note
(which links to it) and the inventory list (which offers Delete on it).
Kept outside both route modules so neither imports the other."""
from __future__ import annotations

from urllib.parse import urlencode

# The item list's ?filter= value for the samples that are still removable. The API
# owns its meaning (query_items, celerp.services.demo.untouched_demo_item_ids): seeded
# by setup and never edited or used, so a changed sample or an item the owner named
# "[DEMO] ..." is never listed here. This string mirrors the API's.
DEMO_ITEMS_FILTER = "demo"
# The dashboard demo note's link: that list, with the hint at the select-all box.
DEMO_ITEMS_URL = "/inventory?" + urlencode({"filter": DEMO_ITEMS_FILTER, "hint": "demo"})
