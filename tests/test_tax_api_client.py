# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The app's client for the tax and localization endpoints: each call sends its method, path
and body, and hands back the API's answer as it came."""
from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest

import ui.api_client as api

pytestmark = pytest.mark.asyncio

_CALLS = {
    "localization_preview": ((), "POST", "/settings/taxes/localization/preview", None),
    "localization_confirm": (("f1", "k1"), "POST", "/settings/taxes/localization/confirm",
                             {"binding": "f1", "idempotency_key": "k1"}),
    "localization_undo": (("op-1",), "POST", "/settings/taxes/localization/undo", {"operation_id": "op-1"}),
    "rate_review_get": (("op-1",), "GET", "/settings/taxes/rate-review/op-1", None),
    "rate_review_apply": (("op-1", ["i-1"], "r42"), "POST", "/settings/taxes/rate-review/op-1/apply",
                          {"item_ids": ["i-1"], "binding": "r42"}),
    "rate_review_undo": (("op-1",), "POST", "/settings/taxes/rate-review/op-1/undo", None),
    "dismiss_tax_upgrade_notice": ((), "POST", "/settings/taxes/upgrade-notice/dismiss", None),
    "withholding_corrections": ((), "GET", "/settings/taxes/withholding-corrections", None),
    "withholding_correct": (("doc:b1", "2026-10-15"), "POST", "/settings/taxes/withholding-corrections/doc:b1",
                            {"date": "2026-10-15"}),
    "withholding_correction_undo": (("doc:b1",), "POST", "/settings/taxes/withholding-corrections/doc:b1/undo",
                                    None),
    "patch_taxes": (([{"name": "VAT"}], "fp"), "PATCH", "/companies/me/taxes",
                    {"taxes": [{"name": "VAT"}], "fingerprint": "fp"}),
    "patch_purchasing_taxes": (([{"name": "VAT"}], "fp"), "PATCH", "/companies/me/purchasing-taxes",
                               {"taxes": [{"name": "VAT"}], "fingerprint": "fp"}),
}


@pytest.mark.parametrize("name", sorted(_CALLS))
async def test_each_tax_call_sends_its_request_and_returns_the_answer(name):
    args, method, path, body = _CALLS[name]
    sent = []

    def answer(request):
        sent.append(request)
        return httpx.Response(200, json={"answered": name})

    with patch("ui.api_client._get_transport", new=lambda: httpx.MockTransport(answer)):
        assert await getattr(api, name)("tok", *args) == {"answered": name}
    [request] = sent
    assert request.method == method and request.url.path == path
    assert (json.loads(request.content) if request.content else None) == body
