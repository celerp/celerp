# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""What the AI page shows for each answer and account situation.

An answer built from help pages ends with a Sources line linking each page; a help
search that found nothing says the answer is not from the Celerp help.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from fasthtml.common import to_xml
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from celerp.help import registry
from celerp.help.schema import HelpPage
from ui.i18n import t
from test_helpers import make_test_token

_GOOD = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "help" / "good"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    from celerp_ai.ui_routes import setup_ui_routes
    setup_ui_routes(ui_app)
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        c.cookies.update({"celerp_token": make_test_token()})
        yield c


@pytest.fixture
def one_help_page(monkeypatch):
    page = HelpPage(
        id="contacts.add-supplier", title="Add a supplier", summary="Add a vendor.", keywords=[],
        routes=["/contacts"], permission=None, order=1,
        steps=[{"text": "Open the list.", "labels": {}, "image": None,
                "capture": {"route": "/contacts", "selector": "main", "actions": []}}],
        related=[], help_dir=_GOOD, source="first_party", module="celerp-contacts",
        module_display="Contacts",
    )
    monkeypatch.setattr(registry, "_index", registry.Index(pages={page.id: page}))
    return page


@pytest.mark.asyncio
async def test_sources_line_and_no_coverage_line(ui_client, one_help_page):
    from celerp_ai.ui_routes import _thread

    async def _reply(help_pages):
        result = {"answer": "Open Contacts.", "model_used": "m", "tools_called": [],
                  "pending_actions": [], "help_pages": help_pages}
        with patch("celerp_ai.ui_routes.api.ai_conversation_query", AsyncMock(return_value=result)):
            r = await ui_client.post("/ai/chat", data={"query": "supplier?", "conversation_id": "conv-1"})
        assert r.status_code == 200
        return r.text

    cited = await _reply(["contacts.add-supplier"])
    assert t("ai.help_sources") in cited
    assert 'href="/help/contacts.add-supplier"' in cited and "Add a supplier" in cited
    assert t("ai.help_no_coverage") not in cited

    uncovered = await _reply([])
    assert t("ai.help_no_coverage") in uncovered
    assert t("ai.help_sources") not in uncovered

    plain = await _reply(None)
    assert t("ai.help_sources") not in plain and t("ai.help_no_coverage") not in plain

    # A reloaded thread shows the same line from the stored message.
    stored = {"id": "m1", "role": "assistant", "content": "Open Contacts.",
              "created_at": "2026-10-23T00:00:00", "help_pages": ["contacts.add-supplier"]}
    html = "".join(to_xml(ft) for ft in _thread([stored], [], "conv-1", "en"))
    assert 'href="/help/contacts.add-supplier"' in html
