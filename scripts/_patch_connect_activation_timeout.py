from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"expected one anchor in {path}, found {text.count(old)}: {old[:120]!r}")
    p.write_text(text.replace(old, new, 1))


# The local API is itself allowed 10s for /auth/activate and then may spend up to
# ~3s waiting for the freshly-started gateway socket to become active. The UI's
# old 10s outer timeout could therefore cancel a healthy activation before the
# API handler's own bounded work could finish. Keep this timeout scoped only to
# Connect activation rather than widening every interactive request.
replace_once(
    "ui/api_client.py",
    '''async def activate_relay(token: str) -> dict:\n    """POST /settings/cloud-activate — call relay /auth/activate, start gateway."""\n    async with _api_client(token) as c:\n        return _raise(await c.post("/settings/cloud-activate")).json()\n''',
    '''# /settings/cloud-activate may legitimately spend up to 10s reaching the relay\n# and another ~3s waiting for the new gateway connection. Its caller must have a\n# strictly larger deadline or it can manufacture a 504 while activation is still\n# succeeding underneath it. Five seconds of scheduling/network margin keeps the\n# request finite without changing the timeout of ordinary interactive calls.\n_CONNECT_ACTIVATE_TIMEOUT = 18.0\n\n\nasync def activate_relay(token: str) -> dict:\n    """POST /settings/cloud-activate — call relay /auth/activate, start gateway."""\n    async with _api_client(token, timeout=_CONNECT_ACTIVATE_TIMEOUT) as c:\n        return _raise(await c.post("/settings/cloud-activate")).json()\n''',
)

# Name the backend pieces of the nested deadline so the relationship is reviewable
# and regression-testable instead of being hidden as three unrelated magic numbers.
replace_once(
    "celerp/routers/health.py",
    '''    try:\n        async with httpx.AsyncClient(timeout=10.0) as c:\n            r = await c.post(f"{relay_base}/auth/activate", json=activate_payload(iid))\n''',
    '''    try:\n        async with httpx.AsyncClient(timeout=_RELAY_ACTIVATE_TIMEOUT) as c:\n            r = await c.post(f"{relay_base}/auth/activate", json=activate_payload(iid))\n''',
)
replace_once(
    "celerp/routers/health.py",
    '''        for _ in range(15):\n            if gw and gw.relay_status == "active":\n                break\n            await asyncio.sleep(0.2)\n''',
    '''        for _ in range(_GATEWAY_START_ATTEMPTS):\n            if gw and gw.relay_status == "active":\n                break\n            await asyncio.sleep(_GATEWAY_START_INTERVAL)\n''',
)
# Insert constants immediately before _apply_gateway_token_api, after imports/helpers.
replace_once(
    "celerp/routers/health.py",
    '''async def _apply_gateway_token_api(token: str, iid: str, public_url: str | None = None, tos_version: str | None = None) -> None:\n''',
    '''# Connect activation has nested finite waits: relay HTTP first, then a short\n# best-effort wait for the gateway socket. ui.api_client intentionally gives the\n# outer request a larger budget. Keep these named so tests can enforce that\n# ordering whenever either side changes.\n_RELAY_ACTIVATE_TIMEOUT = 10.0\n_GATEWAY_START_ATTEMPTS = 15\n_GATEWAY_START_INTERVAL = 0.2\n\n\nasync def _apply_gateway_token_api(token: str, iid: str, public_url: str | None = None, tos_version: str | None = None) -> None:\n''',
)

Path("tests/test_connect_activation_timeout.py").write_text('''# Copyright (c) 2026 Noah Severs\n# SPDX-License-Identifier: LicenseRef-Proprietary\n"""Regression guards for the Connect activation deadline contract."""\nfrom __future__ import annotations\n\nfrom contextlib import asynccontextmanager\nfrom unittest.mock import AsyncMock, MagicMock\n\nimport pytest\n\nimport ui.api_client as api\nfrom celerp.routers import health\n\n\ndef test_connect_outer_deadline_exceeds_backend_worst_case():\n    backend_budget = (\n        health._RELAY_ACTIVATE_TIMEOUT\n        + health._GATEWAY_START_ATTEMPTS * health._GATEWAY_START_INTERVAL\n    )\n    assert api._CONNECT_ACTIVATE_TIMEOUT > backend_budget\n    assert api._CONNECT_ACTIVATE_TIMEOUT - backend_budget >= 4.0\n\n\n@pytest.mark.asyncio\nasync def test_activate_relay_uses_connect_specific_timeout(monkeypatch):\n    seen = {}\n    response = MagicMock()\n    response.is_redirect = False\n    response.is_error = False\n    response.json.return_value = {"connected": True}\n    client = MagicMock()\n    client.post = AsyncMock(return_value=response)\n\n    @asynccontextmanager\n    async def fake_api_client(token, timeout=10.0):\n        seen["token"] = token\n        seen["timeout"] = timeout\n        yield client\n\n    monkeypatch.setattr(api, "_api_client", fake_api_client)\n    result = await api.activate_relay("access-token")\n\n    assert result == {"connected": True}\n    assert seen == {"token": "access-token", "timeout": api._CONNECT_ACTIVATE_TIMEOUT}\n    client.post.assert_awaited_once_with("/settings/cloud-activate")\n''')

print("Connect timeout patch applied")
