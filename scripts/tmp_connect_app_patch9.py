from pathlib import Path

p = Path("celerp/routers/health.py")
text = p.read_text()
old = 'return {"error": f"Connection to {relay_base} timed out or could not be reached."}'
new = ('return {"error": f"Connection to {relay_base} timed out or could not be reached. "'
       '                "Check your internet connection or firewall and try again."}')
count = text.count(old)
if count != 2:
    raise RuntimeError(f"celerp/routers/health.py: expected 2 relay timeout messages, got {count}")
p.write_text(text.replace(old, new))
