from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


# config.toml uses an explicit cloud-key serializer. Persist the verifier so the
# proof survives process restart until a credential is durably applied.
replace_once(
    "celerp/config.py",
    '''        if cloud.get("disconnected"):\n            lines.append("disconnected = true")\n''',
    '''        if cloud.get("activation_verifier"):\n            lines.append(f'activation_verifier = {_str(cloud["activation_verifier"])}')\n        if cloud.get("disconnected"):\n            lines.append("disconnected = true")\n''',
)

# Regression: prove generation -> disk -> module reload preserves one verifier,
# and authoritative credential persistence removes it from disk and memory.
p = Path("tests/test_instance_identity.py")
p.write_text(p.read_text() + r'''


def test_activation_verifier_survives_restart_until_credential_is_durable(tmp_path, monkeypatch):
    mod, _ = _reload_config(tmp_path, monkeypatch)
    iid = mod.ensure_instance_id()
    verifier = mod.ensure_activation_verifier()
    assert verifier
    assert mod.read_config()["cloud"]["activation_verifier"] == verifier

    importlib.reload(mod)
    mod.load_cloud_config()
    assert mod.ensure_instance_id() == iid
    assert mod.ensure_activation_verifier() == verifier

    mod.record_cloud_activation("gw-token", iid, public_url=None)
    cfg = mod.read_config()
    assert cfg["cloud"]["token"] == "gw-token"
    assert "activation_verifier" not in cfg["cloud"]
    assert "public_url" not in cfg["cloud"]
    assert mod.settings.activation_verifier == ""
''')
