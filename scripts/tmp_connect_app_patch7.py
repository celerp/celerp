from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


# A paid public URL already requires the gateway, so do not open a database
# session just to ask whether a public share also exists. The share lookup is
# needed only on the free/no-URL path to decide whether the gateway may stop.
replace_once(
    "celerp/routers/health.py",
    '''    from celerp.gateway import ensure_running, has_active_share\n    active_share = await has_active_share()\n    existing = _gw.get_client()\n    should_serve = bool(_s.celerp_public_url or active_share)\n''',
    '''    from celerp.gateway import ensure_running, has_active_share\n    should_serve = bool(_s.celerp_public_url)\n    if not should_serve:\n        should_serve = await has_active_share()\n    existing = _gw.get_client()\n''',
)
