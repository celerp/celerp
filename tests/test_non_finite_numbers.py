# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A number that is not finite (inf, nan) never reaches the API as a request body: the local
client refuses it as invalid input, so a route that parsed one shows "invalid input" instead of
answering 500, and the labels editor reads it as unreadable, like any other bad text."""
from __future__ import annotations

import math

import pytest

from ui import api_client as api
from ui.api_client import APIError
from ui.i18n import t

_NON_FINITE = (math.inf, -math.inf, math.nan)


@pytest.mark.parametrize("value", _NON_FINITE, ids=["inf", "-inf", "nan"])
async def test_local_client_refuses_non_finite_numbers(value):
    async with api._local_client("tok") as client:
        with pytest.raises(APIError) as caught:
            client.build_request("PATCH", "/docs/doc:1", json={"line_items": [{"quantity": value}]})
        assert caught.value.status == 422
        assert api.error_message(caught.value) == t("error.invalid_input")
        # A finite number and a body that is not JSON still build as before.
        assert client.build_request("PATCH", "/docs/doc:1", json={"quantity": 1.5}).method == "PATCH"
        assert client.build_request("POST", "/x", content=b"inf").method == "POST"
    # Every API call that goes through the shared client context managers refuses it too,
    # before anything is sent.
    with pytest.raises(APIError) as caught:
        await api.create_doc("tok", {"doc_type": "invoice", "line_items": [{"quantity": value}]})
    assert caught.value.status == 422


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "Infinity", "1e309"])
def test_label_numbers_refuse_non_finite(raw):
    from celerp_labels.service import LabelNumberError, label_number
    with pytest.raises(LabelNumberError):
        label_number("width_mm", raw)
    assert label_number("width_mm", "25.5") == 25.5


@pytest.mark.parametrize("raw", ["1e309", "inf", "nan"])
async def test_writeoff_qty_rejects_non_finite(raw):
    """The write-off quantity cell answers "Enter a number" in place, keeping what was typed."""
    import html
    from unittest.mock import AsyncMock, patch

    from httpx import ASGITransport, AsyncClient

    from test_helpers import authed_cookies
    from ui.app import app as ui_app
    save = AsyncMock(return_value={})
    with patch("ui.api_client.set_writeoff_line", new=save), \
         patch("ui.api_client.get_list", new=AsyncMock(return_value={"id": "list:W1", "line_items": []})):
        async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
            ui.cookies.update(authed_cookies("owner"))
            r = await ui.patch("/lists/list:W1/writeoff-line/L1/qty_out", data={"value": raw},
                               headers={"HX-Request": "true"})
    assert t("documents.enter_a_number") in html.unescape(r.text)
    assert f'value="{raw}"' in r.text
    save.assert_not_called()
