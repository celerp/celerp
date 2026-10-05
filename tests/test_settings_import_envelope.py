# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The tax and payment-term CSV imports post records the API accepts.

Both confirm handlers send their rows to a settings batch-import endpoint whose
body is ``SettingsBatchImportRequest``. Each posted body is validated against that
model here, so a handler that posts bare rows (which the API rejects with 422 and
the page shows as "0 created, 1 error") fails.
"""

from unittest.mock import AsyncMock

import pytest

from celerp.routers.companies import SettingsBatchImportRequest
from ui.routes import settings_import as si
from ui.routes.csv_import import stash_import_csv


class _App:
    def __init__(self):
        self.handlers: dict = {}

    def get(self, path):
        return lambda fn: fn

    def post(self, path):
        def deco(fn):
            self.handlers[path] = fn
            return fn
        return deco


class _Req:
    def __init__(self, form: dict):
        self._form = form
        self.cookies: dict = {}

    async def form(self):
        return self._form


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,csv_text,field", [
    ("taxes", "name,rate,tax_type,is_default,description\nCSV Tax 7,7,both,false,Seven\n", "rate"),
    ("payment-terms", "name,days,description\nNet 45,45,Forty-five days\n", "days"),
])
async def test_confirm_posts_records_the_api_accepts(monkeypatch, kind, csv_text, field):
    posted: list = []

    async def _batch(token, path, records):
        posted.append((path, records))
        return {"created": len(records), "skipped": 0, "errors": []}

    monkeypatch.setattr(si, "_token", lambda request: "tok")
    monkeypatch.setattr(si.api, "batch_import", _batch)
    monkeypatch.setattr(si.api, "get_company", AsyncMock(return_value={"id": "c1"}))
    app = _App()
    si.setup_routes(app)
    ref = await stash_import_csv("tok", csv_text)
    await app.handlers[f"/settings/import/{kind}/confirm"](_Req({"csv_ref": ref}))

    [(path, records)] = posted
    assert path == f"/companies/me/{kind}/import/batch"
    body = SettingsBatchImportRequest.model_validate({"records": records})
    [rec] = body.records
    assert rec.data["name"] == csv_text.splitlines()[1].split(",")[0]
    assert rec.data[field]
