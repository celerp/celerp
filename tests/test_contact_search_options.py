# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for the contact picker server-side search.

Covers:
  1. searchable_select() HTML output when search_url is provided.
  2. GET /contacts/search-options endpoint — empty q, with q, field variants.
"""
from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from test_helpers import make_test_token


def _authed() -> dict:
    return {"celerp_token": make_test_token()}


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(
        transport=ASGITransport(app=ui_app),
        base_url="http://ui",
        follow_redirects=False,
    ) as c:
        yield c


# ── searchable_select component tests ────────────────────────────────────────

class TestSearchableSelectComponent:

    def _render(self, ft) -> str:
        from fasthtml.common import to_xml
        return to_xml(ft)

    def test_no_search_url_no_htmx_attrs_on_wrap(self):
        """Without search_url, wrap div must have no hx-get attr."""
        from ui.components.table import searchable_select
        html = self._render(searchable_select("field", [("v1", "Label 1")]))
        assert 'hx-get' not in html or 'combobox-list' not in html.split('hx-get')[1].split('>')[0]

    def test_search_url_adds_htmx_attrs_to_wrap(self):
        """With search_url, wrap div must have hx-get, hx-trigger, hx-target, hx-swap, hx-include."""
        from ui.components.table import searchable_select
        html = self._render(searchable_select(
            "field",
            [("v1", "Label 1")],
            search_url="/contacts/search-options?contact_type=customer&field=contact_id",
        ))
        assert 'hx-get="/contacts/search-options' in html
        assert 'hx-trigger=' in html
        assert '.combobox-input' in html
        assert 'hx-target=' in html
        assert 'hx-swap="innerHTML"' in html
        assert 'data-search-url="1"' in html
        # hx-include must point to the text input so HTMX sends q= in the request
        assert 'hx-include=' in html
        assert 'combobox-input' in html.split('hx-include=')[1].split('"')[1]

    def test_search_url_adds_name_q_to_text_input(self):
        """With search_url, the visible text input must have name="q"."""
        from ui.components.table import searchable_select
        html = self._render(searchable_select(
            "field",
            [("v1", "Label 1")],
            search_url="/contacts/search-options",
        ))
        # The combobox-input must carry name="q" for HTMX to include its value
        assert 'name="q"' in html

    def test_no_search_url_text_input_has_no_name_q(self):
        """Without search_url, the visible text input must not have name="q"."""
        from ui.components.table import searchable_select
        html = self._render(searchable_select("field", [("v1", "Label 1")]))
        # name="q" should only appear on the hidden input if it's named "q"; for other names it's absent
        assert 'name="q"' not in html or 'type="hidden"' in html.split('name="q"')[0].rsplit('<', 1)[-1]

    def test_static_options_still_rendered_with_search_url(self):
        """Static options must be in the initial HTML even when search_url is set."""
        from ui.components.table import searchable_select
        html = self._render(searchable_select(
            "field",
            [("id-1", "Alice"), ("id-2", "Bob")],
            search_url="/contacts/search-options",
        ))
        assert "Alice" in html
        assert "Bob" in html
        assert 'data-value="id-1"' in html
        assert 'data-value="id-2"' in html

    def test_htmx_attrs_still_on_hidden_input(self):
        """HTMX patch/trigger attrs must stay on the hidden input (not moved to wrap)."""
        from ui.components.table import searchable_select
        html = self._render(searchable_select(
            "value",
            [("id-1", "Alice")],
            search_url="/contacts/search-options",
            hx_patch="/docs/doc:1/field/contact_id",
            hx_trigger="change",
        ))
        assert 'hx-patch="/docs/doc:1/field/contact_id"' in html
        assert 'hx-trigger="change"' in html


# ── /contacts/search-options endpoint tests ──────────────────────────────────

class TestContactSearchOptionsEndpoint:

    @pytest.mark.asyncio
    async def test_empty_q_returns_empty_body(self, ui_client):
        """GET /contacts/search-options without q → 200 with empty body."""
        r = await ui_client.get(
            "/contacts/search-options",
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert r.content == b""

    @pytest.mark.asyncio
    async def test_blank_q_returns_empty_body(self, ui_client):
        """GET /contacts/search-options?q= (whitespace only) → empty body."""
        r = await ui_client.get(
            "/contacts/search-options?q=   ",
            cookies=_authed(),
        )
        assert r.status_code == 200
        assert r.content.strip() == b""

    @pytest.mark.asyncio
    async def test_unauthenticated_redirects_to_login(self, ui_client):
        """Without auth cookie → redirected to login by the auth wall."""
        r = await ui_client.get("/contacts/search-options?q=alice")
        assert r.status_code in (302, 303)
        assert "/login" in r.headers.get("location", "")

    @pytest.mark.asyncio
    async def test_with_q_returns_option_html(self, ui_client):
        """GET /contacts/search-options?q=alice → HTML combobox-option fragments."""
        contacts = [
            {"entity_id": "ct:1", "name": "Alice Smith", "company_name": "ACME"},
            {"entity_id": "ct:2", "name": "Alice Jones", "company_name": ""},
        ]
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": 2})):
            r = await ui_client.get(
                "/contacts/search-options?q=alice&contact_type=customer",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"combobox-option" in r.content
        assert b"Alice Smith" in r.content
        assert b"Alice Jones" in r.content
        assert b'data-value="ct:1"' in r.content
        assert b'data-value="ct:2"' in r.content

    @pytest.mark.asyncio
    async def test_default_field_includes_add_new_option(self, ui_client):
        """contact_id field variant includes '+ Add new contact' option."""
        contacts = [{"entity_id": "ct:1", "name": "Alice"}]
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": 1})):
            r = await ui_client.get(
                "/contacts/search-options?q=alice",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"__new__" in r.content
        assert b"Add new contact" in r.content

    @pytest.mark.asyncio
    async def test_contact_company_name_field_excludes_add_new(self, ui_client):
        """contact_company_name field variant must NOT include '+ Add new contact'."""
        contacts = [{"entity_id": "ct:1", "name": "Alice", "company_name": "ACME Ltd"}]
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": 1})):
            r = await ui_client.get(
                "/contacts/search-options?q=acme&field=contact_company_name",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"ACME Ltd" in r.content
        assert b"__new__" not in r.content

    @pytest.mark.asyncio
    async def test_contact_company_name_field_uses_company_name_as_label(self, ui_client):
        """contact_company_name variant shows company_name, not contact name."""
        contacts = [{"entity_id": "ct:1", "name": "Alice Smith", "company_name": "Globex Corp"}]
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": 1})):
            r = await ui_client.get(
                "/contacts/search-options?q=globex&field=contact_company_name",
                cookies=_authed(),
            )
        assert b"Globex Corp" in r.content
        # The entity_id is the value (for resolving to contact_id)
        assert b'data-value="ct:1"' in r.content

    @pytest.mark.asyncio
    async def test_contact_company_name_skips_contacts_without_company_name(self, ui_client):
        """contact_company_name variant must exclude contacts without company_name."""
        contacts = [
            {"entity_id": "ct:1", "name": "Alice", "company_name": "ACME"},
            {"entity_id": "ct:2", "name": "Bob", "company_name": ""},
            {"entity_id": "ct:3", "name": "Carol", "company_name": None},
        ]
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": contacts, "total": 3})):
            r = await ui_client.get(
                "/contacts/search-options?q=a&field=contact_company_name",
                cookies=_authed(),
            )
        assert b"ACME" in r.content
        assert b'data-value="ct:2"' not in r.content
        assert b'data-value="ct:3"' not in r.content

    @pytest.mark.asyncio
    async def test_invalid_contact_type_defaults_to_customer(self, ui_client):
        """contact_type values other than customer/vendor fall back to customer."""
        captured: list[dict] = []

        async def mock_list(token, params):
            captured.append(params)
            return {"items": [], "total": 0}

        with patch("ui.api_client.list_contacts", new=AsyncMock(side_effect=mock_list)):
            r = await ui_client.get(
                "/contacts/search-options?q=test&contact_type=hacker",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert captured[0].get("contact_type") == "customer"

    @pytest.mark.asyncio
    async def test_no_results_shows_empty_option(self, ui_client):
        """When search returns no contacts, the no-results option is visible."""
        with patch("ui.api_client.list_contacts", new=AsyncMock(return_value={"items": [], "total": 0})):
            r = await ui_client.get(
                "/contacts/search-options?q=zzz_nobody",
                cookies=_authed(),
            )
        assert r.status_code == 200
        assert b"combobox-option--empty" in r.content
        # No results option must be visible (no display:none)
        content = r.text
        idx = content.find("combobox-option--empty")
        assert idx >= 0
        snippet = content[max(0, idx - 50):idx + 100]
        assert "display:none" not in snippet

    @pytest.mark.asyncio
    async def test_api_error_returns_empty_fragment(self, ui_client):
        """When the API raises, the endpoint returns only the no-results option."""
        from ui.api_client import APIError
        with patch("ui.api_client.list_contacts", new=AsyncMock(side_effect=APIError(500, "error"))):
            r = await ui_client.get(
                "/contacts/search-options?q=error",
                cookies=_authed(),
            )
        assert r.status_code == 200
        # Still a valid response: just empty options with no-results visible
        assert b"combobox-option--empty" in r.content

    @pytest.mark.asyncio
    async def test_search_passes_no_limit_cap(self, ui_client):
        """The endpoint must not add a limit to the API call (search all contacts)."""
        captured: list[dict] = []

        async def mock_list(token, params):
            captured.append(params)
            return {"items": [], "total": 0}

        with patch("ui.api_client.list_contacts", new=AsyncMock(side_effect=mock_list)):
            await ui_client.get(
                "/contacts/search-options?q=alice",
                cookies=_authed(),
            )
        assert captured, "list_contacts was not called"
        params = captured[0]
        # Must have q but must NOT have a limit key
        assert params.get("q") == "alice"
        assert "limit" not in params