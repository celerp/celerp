"""Values of the wrong shape in a request body are refused with an explanation, never a server error."""
from __future__ import annotations

import pytest

from test_runs import _contributed

pytestmark = pytest.mark.asyncio

WRONG = ([1, 2], {"a": 1}, "x", -1, True)


async def test_wrong_shaped_values_are_refused_with_an_explanation(fa):
    car = await _contributed(fa)
    cls = (await fa.classes())["Vehicles"]["id"]
    calls = []
    for value in WRONG:
        calls += [
            ("POST", f"/classes/{cls}/fix-account", {"field": value, "mode": "move", "account": "1500"}),
            ("POST", f"/classes/{cls}/fix-account", {"field": "asset_account", "mode": value, "account": "1500"}),
            ("POST", f"/assets/{car}/reminders", {"kind": value, "due": "2026-06-01"}),
        ]
        if not isinstance(value, dict):
            calls += [
                ("POST", f"/classes/{cls}/valuer-report/remove", {"files": value}),
                ("PATCH", "/reminders/nope", {"changes": value}),
                ("PATCH", "/maintenance/nope", {"changes": value}),
            ]
    calls.append(("POST", f"/classes/{cls}/valuer-report/remove", {"files": [{"asset_id": car}, "x"]}))
    for method, path, body in calls:
        r = await fa.client.request(method, f"/fixed-assets/api{path}", headers=fa.h, json=body)
        assert r.status_code == 422, (method, path, body, r.status_code, r.text)
        assert isinstance(r.json()["detail"], str) and r.json()["detail"], (method, path, body)
