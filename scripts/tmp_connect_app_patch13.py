from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


# Distinguish an authoritative relay entitlement result (where public_url=None
# means paid service was removed) from the legacy apply-token endpoint omitting
# public_url entirely. The latter must not tear down a healthy same-token client
# or erase its last-known URL merely because the payload did not carry that field.
replace_once(
    "celerp/routers/health.py",
    '''async def _apply_gateway_token_api(\n    token: str, iid: str, public_url: str | None = None,\n    tos_version: str | None = None,\n) -> None:\n''',
    '''async def _apply_gateway_token_api(\n    token: str, iid: str, public_url: str | None = None,\n    tos_version: str | None = None, *, authoritative_public_url: bool = True,\n) -> None:\n''',
)
replace_once(
    "celerp/routers/health.py",
    '''    _s.gateway_token = token\n    _s.gateway_instance_id = iid\n    _s.celerp_public_url = public_url or ""\n    _s.cloud_disconnected = False\n''',
    '''    effective_public_url = (\n        public_url if authoritative_public_url else (_s.celerp_public_url or None))\n    _s.gateway_token = token\n    _s.gateway_instance_id = iid\n    _s.celerp_public_url = effective_public_url or ""\n    _s.cloud_disconnected = False\n''',
)
replace_once(
    "celerp/routers/health.py",
    '''    record_cloud_activation(\n        token, iid, public_url=public_url, tos_version=tos_version,\n        backup_encryption_key=_s.backup_encryption_key)\n''',
    '''    record_cloud_activation(\n        token, iid, public_url=effective_public_url, tos_version=tos_version,\n        backup_encryption_key=_s.backup_encryption_key)\n''',
)
replace_once(
    "celerp/routers/health.py",
    '''    existing = _gw.get_client()\n    if existing is not None and (\n            not existing.is_serving(token) or not should_serve):\n        await existing.close()\n        _gw.set_client(None)\n''',
    '''    existing = _gw.get_client()\n    if existing is not None and (\n            not existing.is_serving(token) or\n            (authoritative_public_url and not should_serve)):\n        await existing.close()\n        _gw.set_client(None)\n''',
)
replace_once(
    "celerp/routers/health.py",
    '''    public_url = payload.get("public_url") or None\n    tos_version = payload.get("tos_version") or None\n''',
    '''    public_url_known = "public_url" in payload\n    public_url = payload.get("public_url") or None\n    tos_version = payload.get("tos_version") or None\n''',
)
replace_once(
    "celerp/routers/health.py",
    '''    await _apply_gateway_token_api(token, iid, public_url=public_url, tos_version=tos_version)\n''',
    '''    await _apply_gateway_token_api(\n        token, iid, public_url=public_url, tos_version=tos_version,\n        authoritative_public_url=public_url_known)\n''',
)


# Prove the other side of the distinction too: an explicit null URL from relay
# authority is a downgrade and must stop an otherwise same-token paid tunnel.
p = Path("tests/test_routers/test_cloud_relay.py")
text = p.read_text()
marker = '''\n\ndef test_legacy_relay_toggle_routes_absent():\n'''
if text.count(marker) != 1:
    raise RuntimeError("test_cloud_relay.py: legacy toggle marker mismatch")
extra = r'''

@pytest.mark.asyncio
async def test_cloud_apply_token_explicit_null_url_stops_serving_client(client):
    token = await _register(client, "apply-explicit-downgrade")
    live = MagicMock()
    live.is_serving = MagicMock(return_value=True)
    live.close = AsyncMock()

    from celerp.config import settings as _s
    _s.backup_enabled = False
    _s.celerp_public_url = "https://paid.example.celerp.com"

    with (
        patch("celerp.gateway.ensure_running"),
        patch("celerp.gateway.has_active_share", new=AsyncMock(return_value=False)),
        patch("celerp.gateway.client.get_client", return_value=live),
        patch("celerp.gateway.client.set_client"),
        patch("celerp.config.write_config"),
        patch("celerp.config.read_config", return_value={"cloud": {}}),
    ):
        r = await client.post(
            "/settings/cloud-apply-token",
            headers=_h(token),
            json={"gateway_token": "same-tok", "public_url": None},
        )

    assert r.status_code == 200
    live.close.assert_awaited_once()
    assert _s.celerp_public_url == ""
'''
text = text.replace(marker, extra + marker)
p.write_text(text)
