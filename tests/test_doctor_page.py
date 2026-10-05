# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The Doctor page in the reader's language: the failed start's report, and, for someone
who may not run the record checks, a plain note to ask an admin instead of the permission's name."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from test_helpers import make_test_token
from ui import i18n
from ui.api_client import APIError

pytestmark = pytest.mark.asyncio

_PERMISSION = "Requires the manage_company_settings permission"


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui",
                           follow_redirects=False) as c:
        yield c


async def _doctor_page(ui_client, role: str, start=None, checks=None) -> str:
    with patch("ui.api_client.get_start_report", new=start or AsyncMock(return_value={"held_back": None})), \
         patch("ui.api_client.doctor_report", new=checks or AsyncMock(return_value={"results": []})):
        r = await ui_client.get("/doctor", cookies={"celerp_token": make_test_token(role=role),
                                                    "celerp_lang": "de"})
    assert r.status_code == 200, r.text
    return r.text


async def test_the_failed_start_reads_in_german(ui_client):
    from celerp.held_back import Failure, HeldBack, module_start_step

    cause = HeldBack((Failure(module_start_step("Manufacturing"), "ZeroDivisionError: division by zero"),))
    page = await _doctor_page(ui_client, "owner", start=AsyncMock(return_value={"held_back": cause.report()}))
    assert i18n.t("held_back.title", lang="de") in page
    assert i18n.t("held_back.step.module_start", lang="de", module="Manufacturing") in page
    assert "ZeroDivisionError: division by zero" in page
    assert "Restart Celerp" not in page and "Starting the Manufacturing module" not in page


async def test_someone_who_may_not_run_the_checks_is_told_to_ask_an_admin(ui_client):
    """Both reports are for admins: a viewer reads, for each, who to ask, never the permission."""
    page = await _doctor_page(ui_client, "viewer", start=AsyncMock(side_effect=APIError(403, _PERMISSION)),
                              checks=AsyncMock(side_effect=APIError(403, _PERMISSION)))
    assert i18n.t("doctor.start_ask_admin", lang="de") in page
    assert i18n.t("doctor.ask_admin", lang="de") in page
    assert _PERMISSION not in page
