from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


# Legacy installs can have a durable instance id but predate activation verifiers.
# Prefer the new observational endpoint. Only an explicit 404 proves the relay is
# old enough to require UUID-only activation, and that compatibility mutation is
# attempted exactly once, never through the retry helper.
replace_once(
    "celerp/main.py",
    '''        if not verifier:\n            # Observation only. This cannot rotate or reveal a gateway credential.\n            try:\n                async with asyncio.timeout(6.0):\n                    async with httpx.AsyncClient(timeout=6.0) as c:\n                        await c.post(\n                            f"{relay_base}/auth/checkin",\n                            json=activate_payload(iid, first_boot=first_boot))\n            except (httpx.HTTPError, TimeoutError):\n                pass\n            return\n\n        # Challenge redemption is idempotent for this verifier, so transient\n        # transport retries are safe here.\n        from celerp.gateway.state import relay_post_with_retry\n        r = await relay_post_with_retry(\n            f"{relay_base}/auth/activate",\n            activate_payload(\n                iid, first_boot=first_boot, activation_verifier=verifier))\n        if r is None or r.status_code != 200:\n            return\n''',
    '''        if not verifier:\n            # Legacy installations may predate challenge-bound activation. New\n            # relays expose an observation-only check-in, so UUID knowledge never\n            # becomes credential authority there. If and only if that endpoint is\n            # absent (404), make one compatibility activation call for an old\n            # relay. Never retry this mutating legacy operation after ambiguity.\n            try:\n                async with asyncio.timeout(6.0):\n                    async with httpx.AsyncClient(timeout=6.0) as c:\n                        checkin = await c.post(\n                            f"{relay_base}/auth/checkin",\n                            json=activate_payload(iid, first_boot=first_boot))\n                        if checkin.status_code != 404:\n                            return\n                        r = await c.post(\n                            f"{relay_base}/auth/activate",\n                            json=activate_payload(iid, first_boot=first_boot))\n            except (httpx.HTTPError, TimeoutError):\n                return\n        else:\n            # Challenge redemption is idempotent for this verifier, so transient\n            # transport retries are safe here.\n            from celerp.gateway.state import relay_post_with_retry\n            r = await relay_post_with_retry(\n                f"{relay_base}/auth/activate",\n                activate_payload(\n                    iid, first_boot=first_boot, activation_verifier=verifier))\n\n        if r is None or r.status_code != 200:\n            return\n''',
)


# Lock the rollout contract into the startup probe tests. These fixtures model an
# installation created before activation_verifier existed by pre-seeding only the
# stable instance id.
p = Path("tests/test_instance_identity.py")
text = p.read_text()
marker = '''\n\nclass TestStickyDisconnect:\n'''
if text.count(marker) != 1:
    raise RuntimeError("tests/test_instance_identity.py: TestStickyDisconnect marker mismatch")
legacy_tests = r'''

    @respx.mock
    async def test_legacy_without_verifier_uses_observational_checkin(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        legacy_iid = "00000000-0000-4000-8000-000000000001"
        mod.write_config({"cloud": {"instance_id": legacy_iid}})
        mod.settings.gateway_instance_id = legacy_iid
        mod.settings.activation_verifier = ""
        checkin = respx.post("https://relay.test/auth/checkin").respond(200, json={"ok": True})
        activate = respx.post("https://relay.test/auth/activate").respond(
            200, json={"gateway_token": "must-not-be-requested"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 0

    @respx.mock
    async def test_legacy_old_relay_404_falls_back_to_one_activation(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        legacy_iid = "00000000-0000-4000-8000-000000000002"
        mod.write_config({"cloud": {"instance_id": legacy_iid}})
        mod.settings.gateway_instance_id = legacy_iid
        mod.settings.activation_verifier = ""
        checkin = respx.post("https://relay.test/auth/checkin").respond(404)
        activate = respx.post("https://relay.test/auth/activate").respond(
            404, json={"detail": "no sub"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 1

    @respx.mock
    async def test_legacy_checkin_transport_ambiguity_never_activates(self, tmp_path, monkeypatch):
        mod, _ = self._prepare(tmp_path, monkeypatch)
        legacy_iid = "00000000-0000-4000-8000-000000000003"
        mod.write_config({"cloud": {"instance_id": legacy_iid}})
        mod.settings.gateway_instance_id = legacy_iid
        mod.settings.activation_verifier = ""
        checkin = respx.post("https://relay.test/auth/checkin")
        checkin.side_effect = httpx.ReadTimeout("ambiguous check-in")
        activate = respx.post("https://relay.test/auth/activate").respond(
            200, json={"gateway_token": "must-not-be-requested"})
        from celerp.main import _try_auto_activate
        await _try_auto_activate()
        assert checkin.call_count == 1
        assert activate.call_count == 0
'''
text = text.replace(marker, legacy_tests + marker)
p.write_text(text)
