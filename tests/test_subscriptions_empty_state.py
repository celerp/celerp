# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""An empty subscriptions list uses the shared empty-state card, with the page's own
New Subscription action, the way the documents and lists pages do."""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.subscriptions import _sub_table


@pytest.mark.parametrize("direction", ["sales", "purchasing"])
def test_empty_list_shows_the_shared_card_with_new_subscription(direction):
    i18n.set_lang("en")
    html = to_xml(_sub_table([], direction))
    assert 'class="empty-state-cta"' in html
    assert i18n.t("label.no_subscription_templates_found") in html
    assert f'hx-post="/subscriptions/new?direction={direction}"' in html
    assert 'class="empty-state-cta-btn"' in html and i18n.t("page.new_subscription") in html
