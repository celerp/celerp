from pathlib import Path

p = Path("celerp/routers/health.py")
text = p.read_text()
needle = "    from celerp.config import ensure_instance_id\n"
if text.count(needle) != 3:
    raise RuntimeError(f"health.py: expected 3 plain ensure_instance_id imports, got {text.count(needle)}")
start = text.index('@settings_router.post("/account-signup"')
end = text.index('@settings_router.get("/account-status")', start)
before, target, after = text[:start], text[start:end], text[end:]
marker = "    from celerp.config import (ensure_instance_id)\n"
changed = before.count(needle) + after.count(needle)
if changed != 2 or target.count(needle) != 1:
    raise RuntimeError("health.py: account-signup import context changed")
before = before.replace(needle, marker)
after = after.replace(needle, marker)
p.write_text(before + target + after)
