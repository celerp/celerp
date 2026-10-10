# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The company preferences on Settings > Company read in the user's language: the shown
value, the choices offered when it is clicked, and the value shown once it is saved."""

from __future__ import annotations

import pytest

from migration_support import OWNER_EMAIL, OWNER_PASSWORD, real_client, real_engine  # noqa: F401 - fixtures
from test_company_backup_ui import _page, ui  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio

_SHOWN = {"en": ("Last 12 months", "50 per page", "100 per page"),
          "de": ("Letzte 12 Monate", "50 pro Seite", "100 pro Seite")}


@pytest.fixture
async def signed_in(ui, real_client):  # noqa: F811
    r = await real_client.post("/auth/register", json={
        "company_name": "Muster GmbH", "email": OWNER_EMAIL, "name": "Owner", "password": OWNER_PASSWORD})
    assert r.status_code == 200, r.text
    ui.cookies.set("celerp_token", r.json()["access_token"])
    return ui


@pytest.mark.parametrize("lang", ["en", "de"])
async def test_the_preferences_read_in_the_users_language(signed_in, lang):
    signed_in.cookies.set("celerp_lang", lang)
    preset, per_page, saved = _SHOWN[lang]

    page = _page(await signed_in.get("/settings/general?tab=company"))
    assert f'<span class="cell-text">{preset}</span>' in page
    assert f'<span class="cell-text">{per_page}</span>' in page

    choices = _page(await signed_in.get("/settings/preferences/default_per_page/edit"))
    assert f'<option value="100">{saved}</option>' in choices
    assert preset in _page(await signed_in.get("/settings/preferences/docs_default_preset/edit"))

    r = await signed_in.patch("/settings/preferences/default_per_page", data={"value": "100"})
    assert f'<span class="cell-text">{saved}</span>' in _page(r)
