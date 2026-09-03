# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The closed-memo badge supersedes the fulfillment badge: a closed memo reads
"Closed", never "partially fulfilled", even though its fulfillment_status stays
"partial"."""

from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from ui.i18n import t
from ui.routes.documents import _render_fulfillment_badge


def _render(ft) -> str:
    from fasthtml.common import to_xml
    return to_xml(ft) if ft is not None else ""


def test_closed_badge_supersedes_fulfillment():
    closed = _render(_render_fulfillment_badge({"status": "closed", "fulfillment_status": "partial"}))
    assert t("doc.closed") in closed
    assert t("doc.partially_fulfilled") not in closed

    # A live, partially-fulfilled memo still shows the partial badge (no regression).
    live = _render(_render_fulfillment_badge({"status": "final", "fulfillment_status": "partial"}))
    assert t("doc.partially_fulfilled") in live
