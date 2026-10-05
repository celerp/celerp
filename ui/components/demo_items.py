# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The inventory search that lists setup's sample items, shared by the dashboard's
demo note (which links to it) and the inventory list (which offers Delete on it).
Kept outside both route modules so neither imports the other."""
from __future__ import annotations

from urllib.parse import urlencode

# Every sample is named "[DEMO] ...". Scoped to the name, because a sample its owner
# renamed still says [DEMO] in its description and is theirs now.
DEMO_ITEMS_QUERY = "name:[DEMO]"
# The dashboard demo note's link: that search, with the hint at the select-all box.
DEMO_ITEMS_URL = "/inventory?" + urlencode({"q": DEMO_ITEMS_QUERY, "hint": "demo"})
