from pathlib import Path

p = Path("celerp/routers/health.py")
text = p.read_text()
old = '''                body = activate_payload(\n                    iid, activation_verifier=verifier or None)\n'''
new = '''                body = activate_payload(\n                    iid, activation_verifier=None if headers else (verifier or None))\n'''
if text.count(old) != 1:
    raise RuntimeError(f"celerp/routers/health.py: expected one reconnect payload, got {text.count(old)}")
p.write_text(text.replace(old, new))
