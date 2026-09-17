from pathlib import Path
import re

p = Path("celerp/routers/health.py")
text = p.read_text()
pattern = r'''(async def cloud_claim_api\(payload: dict\) -> dict:.*?except \(httpx\.ConnectError, TimeoutError\):\n        return \{"error": \(\n            f"Connection to \{relay_base\} timed out before the relay confirmed the link\. "\n            "Try again, or restart Celerp: if the link already went through, "\n            "the saved activation proof recovers it safely on startup\."\)\}\n    except httpx\.TimeoutException:\n)        return \{"error": f"Connection to \{relay_base\} timed out\."\}'''
replacement = r'''\1        return {"error": (
            f"Connection to {relay_base} timed out before the relay confirmed the link. "
            "Try again, or restart Celerp: if the link already went through, "
            "the saved activation proof recovers it safely on startup.")}'''
new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
if n != 1:
    raise RuntimeError(f"celerp/routers/health.py: claim timeout branch matched {n} times")
p.write_text(new)
