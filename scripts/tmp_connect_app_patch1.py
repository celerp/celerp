from pathlib import Path
import re

ROOT = Path('.')


def replace_once(path: str, old: str, new: str) -> None:
    p = ROOT / path
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f'{path}: expected one match, got {text.count(old)}')
    p.write_text(text.replace(old, new))


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = ROOT / path
    text = p.read_text()
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f'{path}: regex matched {n} times')
    p.write_text(new)


# Persist the verifier before any account proof. Fresh installs create it with
# their instance id; existing installs recover it from disk before generating.
replace_once(
    'celerp/config.py',
    '''    gateway_instance_id: str = ""\n    # HTTP base URL for relay API calls (quota, etc.).\n''',
    '''    gateway_instance_id: str = ""\n    # Local secret used only to redeem an email/Google-approved activation.\n    # The relay sees SHA-256(verifier), never this value. Persisted until the\n    # returned gateway credential is safely written, so response loss/restart is\n    # retry-safe and API workers converge on one proof.\n    activation_verifier: str = ""\n    # HTTP base URL for relay API calls (quota, etc.).\n''')

replace_once(
    'celerp/config.py',
    '''    import uuid as _uuid\n    iid = str(_uuid.uuid4())\n    settings.gateway_instance_id = iid\n\n    # Persist to config.toml, creating it when missing - the id must survive\n    # restarts (best-effort; silently skip on error)\n    try:\n        persist_cloud_settings(instance_id=iid)\n''',
    '''    import secrets as _secrets\n    import uuid as _uuid\n    iid = str(_uuid.uuid4())\n    settings.gateway_instance_id = iid\n    if not settings.activation_verifier:\n        settings.activation_verifier = _secrets.token_urlsafe(32)\n\n    # Persist identity and proof secret together. A fresh install therefore has\n    # one verifier before multiple API workers can serve an account-link request.\n    try:\n        persist_cloud_settings(\n            instance_id=iid, activation_verifier=settings.activation_verifier)\n''')

insert_after = '''    return iid\n\n\ndef persist_cloud_settings(**values: str) -> None:\n'''
helpers = '''    return iid\n\n\ndef ensure_activation_verifier() -> str:\n    """Return the durable local activation verifier, creating it once if absent.\n\n    Re-read config before generating so another API worker that already handled\n    the send-code leg wins; the claim leg in this process then uses the same\n    verifier instead of inventing a mismatched proof.\n    """\n    if settings.activation_verifier:\n        return settings.activation_verifier\n    try:\n        cloud = (read_config() or {}).get("cloud", {})\n        stored = cloud.get("activation_verifier")\n        if isinstance(stored, str) and stored:\n            settings.activation_verifier = stored\n            return stored\n    except Exception:\n        pass\n\n    import secrets as _secrets\n    verifier = _secrets.token_urlsafe(32)\n    settings.activation_verifier = verifier\n    persist_cloud_settings(activation_verifier=verifier)\n    return verifier\n\n\ndef activation_challenge(verifier: str | None = None) -> str:\n    """SHA-256 challenge safe to send through account-proof requests."""\n    import hashlib\n    value = verifier or ensure_activation_verifier()\n    return hashlib.sha256(value.encode()).hexdigest()\n\n\ndef record_cloud_activation(\n    gateway_token: str, instance_id: str, *, public_url: str | None = None,\n    tos_version: str | None = None, backup_encryption_key: str | None = None,\n) -> None:\n    """Persist an authoritative activation result in one config write.\n\n    Unlike persist_cloud_settings, this operation intentionally erases a stale\n    paid public URL when the relay returns none, clears sticky disconnect state,\n    and removes the verifier only after the gateway credential is durable.\n    """\n    cfg = read_config()\n    cloud = cfg.setdefault("cloud", {})\n    cloud["token"] = gateway_token\n    cloud["instance_id"] = instance_id\n    if public_url:\n        cloud["public_url"] = public_url\n    else:\n        cloud.pop("public_url", None)\n    if tos_version:\n        cloud["tos_version"] = tos_version\n    if backup_encryption_key:\n        cloud["backup_encryption_key"] = backup_encryption_key\n    cloud.pop("disconnected", None)\n    cloud.pop("activation_verifier", None)\n    write_config(cfg)\n    settings.activation_verifier = ""\n\n\ndef persist_cloud_settings(**values: str) -> None:\n'''
replace_once('celerp/config.py', insert_after, helpers)

replace_once(
    'celerp/config.py',
    '''    if cloud.get("instance_id") and not settings.gateway_instance_id:\n        settings.gateway_instance_id = cloud["instance_id"]\n    if cloud.get("public_url") and not settings.celerp_public_url and not disconnected:\n''',
    '''    if cloud.get("instance_id") and not settings.gateway_instance_id:\n        settings.gateway_instance_id = cloud["instance_id"]\n    if cloud.get("activation_verifier") and not settings.activation_verifier:\n        settings.activation_verifier = cloud["activation_verifier"]\n    if cloud.get("public_url") and not settings.celerp_public_url and not disconnected:\n''')

# Activation payload has one optional proof field; metadata remains single-sourced.
regex_once(
    'celerp/gateway/state.py',
    r'def activate_payload\(instance_id: str, \*, first_boot: bool \| None = None\) -> dict:.*?\n    return payload\n',
    '''def activate_payload(\n    instance_id: str, *, first_boot: bool | None = None,\n    activation_verifier: str | None = None,\n) -> dict:\n    """Build the activation/check-in request metadata.\n\n    activation_verifier is included only for a challenge-approved recovery. The\n    verifier never appears in email/browser proof requests.\n    """\n    import platform as _platform\n\n    from celerp import __version__\n\n    payload = {\n        "instance_id": instance_id,\n        "version": __version__,\n        "platform": _platform.system(),\n        "mode": _launch_mode(),\n    }\n    if first_boot is not None:\n        payload["first_boot"] = first_boot\n    if activation_verifier:\n        payload["activation_verifier"] = activation_verifier\n    return payload\n''')

# Startup never retries the mutating legacy UUID-only activation path. It only
# redeems a persisted verifier (retry-safe on the relay); otherwise it records an
# observational check-in and waits for an explicit account/Connect action.
regex_once(
    'celerp/main.py',
    r'async def _try_auto_activate\(\) -> None:.*?\n\n@asynccontextmanager',
    '''async def _try_auto_activate() -> None:\n    """Recover a challenge-approved activation, otherwise only check in.\n\n    The verifier is durable, so retrying it after response loss returns the same\n    credential. UUID-only activation is intentionally not retried at startup.\n    """\n    _log = logging.getLogger(__name__)\n    try:\n        import httpx\n        from celerp.config import (\n            settings as _s, ensure_instance_id, config_path, record_cloud_activation)\n        if _s.cloud_disconnected:\n            return\n        first_boot = not config_path().exists()\n        iid = ensure_instance_id()\n        from celerp.gateway.state import activate_payload, relay_http_url as _rhu\n        relay_base = _rhu()\n        verifier = _s.activation_verifier or ""\n\n        if not verifier:\n            # Observation only. This cannot rotate or reveal a gateway credential.\n            try:\n                async with asyncio.timeout(6.0):\n                    async with httpx.AsyncClient(timeout=6.0) as c:\n                        await c.post(\n                            f"{relay_base}/auth/checkin",\n                            json=activate_payload(iid, first_boot=first_boot))\n            except (httpx.HTTPError, TimeoutError):\n                pass\n            return\n\n        # Challenge redemption is idempotent for this verifier, so transient\n        # transport retries are safe here.\n        from celerp.gateway.state import relay_post_with_retry\n        r = await relay_post_with_retry(\n            f"{relay_base}/auth/activate",\n            activate_payload(\n                iid, first_boot=first_boot, activation_verifier=verifier))\n        if r is None or r.status_code != 200:\n            return\n        data = r.json()\n        token = data.get("gateway_token", "")\n        if not token:\n            return\n        public_url = data.get("public_url")\n        tos_version = data.get("tos_version")\n        _s.gateway_token = token\n        _s.gateway_instance_id = iid\n        _s.celerp_public_url = public_url or ""\n        if not _s.backup_encryption_key:\n            import base64, secrets as _secrets\n            _s.backup_encryption_key = base64.b64encode(_secrets.token_bytes(32)).decode()\n        try:\n            record_cloud_activation(\n                token, iid, public_url=public_url, tos_version=tos_version,\n                backup_encryption_key=_s.backup_encryption_key)\n        except Exception:\n            # Keep the verifier durable when the credential itself could not be\n            # persisted. A restart can safely redeem the same result again.\n            pass\n\n        from celerp.gateway import ensure_running, has_active_share\n        if public_url or await has_active_share():\n            ensure_running()\n            _log.info("Recovered cloud relay activation (instance_id=%s)", iid)\n        if public_url and _s.backup_enabled and _s.backup_encryption_key:\n            from celerp.services import backup_scheduler\n            backup_scheduler.start()\n    except Exception as exc:\n        logging.getLogger(__name__).debug(\n            "Activation recovery/check-in failed (expected for self-hosted): %s", exc)\n\n\n@asynccontextmanager''')
