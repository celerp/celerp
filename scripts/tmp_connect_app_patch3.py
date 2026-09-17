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


# The background helper existed only to support the now-removed activation task.
# Restore connectors' local fire-and-forget helper so this PR does not broaden
# lifecycle ownership for unrelated connector work.
p = Path('celerp/services/background.py')
if p.exists():
    p.unlink()
replace_once(
    'ui/routes/settings_connectors.py',
    'from celerp.services.background import spawn_background\n',
    '')
replace_once(
    'ui/routes/settings_connectors.py',
    '''async def _kickoff_connector_sync(iid: str, platform: str, token: str) -> None:\n''',
    '''_BG_TASKS: set = set()\n\n\ndef _spawn(coro) -> None:\n    """Fire-and-forget while retaining the task until completion."""\n    import asyncio\n    task = asyncio.create_task(coro)\n    _BG_TASKS.add(task)\n    task.add_done_callback(_BG_TASKS.discard)\n\n\nasync def _kickoff_connector_sync(iid: str, platform: str, token: str) -> None:\n''')
replace_once('ui/routes/settings_connectors.py', 'spawn_background(_do_sync())', '_spawn(_do_sync())')
replace_once('ui/routes/settings_connectors.py', 'spawn_background(_autosync_once(iid, c["id"], token))', '_spawn(_autosync_once(iid, c["id"], token))')

# UI local client has a true total deadline in addition to httpx per-phase bounds.
replace_once(
    'ui/api_client.py',
    '''# Deadline for the claim-flow endpoints (send code, claim). Each must outlast\n# the local API's own worst case: its wait on the relay\n# (celerp.routers.health.RELAY_CLAIM_TIMEOUT) plus the inline activation\n# wait (celerp.routers.health.CLAIM_ACTIVATE_WAIT) plus local overhead, so a\n# slow relay is reported by the API as a relay timeout instead of the UI\n# giving up first.\nCLAIM_TIMEOUT = 15.0\n''',
    '''# Total UI-to-local-API deadline for one claim-flow request. This is an\n# outer wall-clock bound; httpx.Timeout remains the per-phase guard underneath.\nCLAIM_TIMEOUT = 15.0\n''')
replace_once(
    'ui/api_client.py',
    '''async def send_otp(token: str, email: str) -> dict:\n    """POST /settings/cloud-send-otp - send OTP via API process (correct instance_id)."""\n    async with _api_client(token, timeout=CLAIM_TIMEOUT) as c:\n        return _raise(await c.post("/settings/cloud-send-otp", json={"email": email})).json()\n\n\nasync def cloud_claim(token: str, payload: dict) -> dict:\n    """POST /settings/cloud-claim - claim + activate via API process (correct instance_id)."""\n    async with _api_client(token, timeout=CLAIM_TIMEOUT) as c:\n        return _raise(await c.post("/settings/cloud-claim", json=payload)).json()\n''',
    '''async def send_otp(token: str, email: str) -> dict:\n    """Send claim OTP under a true total local-request deadline."""\n    try:\n        async with asyncio.timeout(CLAIM_TIMEOUT):\n            async with _api_client(token, timeout=CLAIM_TIMEOUT) as c:\n                return _raise(\n                    await c.post("/settings/cloud-send-otp", json={"email": email})\n                ).json()\n    except TimeoutError as exc:\n        raise APIError(504, TIMEOUT_MESSAGE) from exc\n\n\nasync def cloud_claim(token: str, payload: dict) -> dict:\n    """Claim/link under a true total local-request deadline."""\n    try:\n        async with asyncio.timeout(CLAIM_TIMEOUT):\n            async with _api_client(token, timeout=CLAIM_TIMEOUT) as c:\n                return _raise(\n                    await c.post("/settings/cloud-claim", json=payload)\n                ).json()\n    except TimeoutError as exc:\n        raise APIError(504, TIMEOUT_MESSAGE) from exc\n''')

# Remove the post-claim polling state machine. A confirmed claim either connects
# inline or shows the normal Connect handoff; restart can also redeem the same
# persisted proof safely.
regex_once(
    'ui/routes/settings.py',
    r'\n    @app.get\("/settings/cloud-link-progress"\).*?\n    @app.get\("/settings/cloud-status"\)',
    '\n    @app.get("/settings/cloud-status")')
replace_once(
    'ui/routes/settings.py',
    '''        if data.get("activating"):\n            # Linked; activation is still running in the API process, so the\n            # tab polls for the connection instead of asking for anything.\n            return _cloud_link_progress(iid, 1)\n\n        # Linked, but activation already gave up: the Connect button retries it.\n        return _cloud_link_handover(iid)\n''',
    '''        # The account link is complete. If bounded activation was not\n        # confirmed inline, Connect (or restart) safely redeems the same durable\n        # proof; there is no background mutation to poll.\n        return _cloud_link_handover(iid)\n''')
regex_once(
    'ui/routes/settings.py',
    r'\n# Polls of /settings/cloud-link-progress.*?\ndef _cloud_relay_unconnected\(',
    '\n\ndef _cloud_relay_unconnected(')
replace_once(
    'ui/routes/settings.py',
    '''    suppress_autoconnect: bool = False,\n    poll: str | None = None,\n) -> FT:\n''',
    '''    suppress_autoconnect: bool = False,\n) -> FT:\n''')
replace_once(
    'ui/routes/settings.py',
    '''        poll: When set, the tab re-fetches this URL every 3s and swaps itself for the\n            response; used while a linked claim's activation is still running.\n''',
    '')
replace_once(
    'ui/routes/settings.py',
    '''    polling = {"hx_get": poll, "hx_trigger": "every 3s", "hx_swap": "outerHTML"} if poll else {}\n    return Div(*children, id="cloud-relay-tab", cls="settings-card", **polling)\n''',
    '''    return Div(*children, id="cloud-relay-tab", cls="settings-card")\n''')

# Remove only the now-dead connecting copy; keep the timeout recovery copy.
for path in Path('ui/locales').glob('*.json'):
    text = path.read_text()
    text, n = re.subn(r'^\s*"settings\.subscription_linked_connecting".*\n', '', text,
                      count=1, flags=re.M)
    if n:
        path.write_text(text)
