"""Celerp counts every rate-limited route on one limiter, keyed per caller (request_caller).

A second slowapi Limiter keyed on the socket address counts every user behind the UI server
as one loopback caller, and the test suite's reset never reaches it, so its counts leak from
one test into the next."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER = ROOT / "celerp" / "routers" / "auth.py"


def test_no_source_builds_a_second_rate_limiter():
    built = [
        f"{path.relative_to(ROOT)}:{n}"
        for top in ("celerp", "ui", "default_modules", "premium_modules") if (ROOT / top).is_dir()
        for path in (ROOT / top).rglob("*.py") if "tests" not in path.parts
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"\bLimiter\(|\bget_remote_address\b", line) and path != OWNER
    ]
    assert built == [], "use celerp.routers.auth.limiter: " + ", ".join(built)
