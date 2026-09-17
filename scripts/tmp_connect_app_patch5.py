from pathlib import Path
import re


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text()
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{path}: regex matched {n} times")
    p.write_text(new)


# Turn the existing serializer into the unlocked primitive. Public writes take the
# same sidecar lock used by activation read-modify-write operations.
replace_once(
    "celerp/config.py",
    "def write_config(cfg: dict) -> None:\n",
    "def _write_config_unlocked(cfg: dict) -> None:\n",
)
replace_once(
    "celerp/config.py",
    '''    path.write_text("\\n".join(lines))\n\n\ndef resolve_install_order''',
    '''    # Crash-safe replacement: fsync a 0600 temp inode, then atomically swap it\n    # over config.toml. Readers see either the complete old file or complete new\n    # file, never torn contents from concurrent workers.\n    import uuid as _uuid\n    data = "\\n".join(lines)\n    tmp = path.with_name(f".{path.name}.{os.getpid()}.{_uuid.uuid4().hex}.tmp")\n    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n    try:\n        with os.fdopen(fd, "w") as f:\n            f.write(data)\n            f.flush()\n            os.fsync(f.fileno())\n        os.replace(tmp, path)\n        from celerp import config_store as _config_store\n        _config_store._fsync_dir(str(path.parent))\n    except Exception:\n        try:\n            if tmp.exists():\n                tmp.unlink()\n        except OSError:\n            pass\n        raise\n\n\ndef _config_lock():\n    """Acquire the cross-process config.toml writer lock or fail closed."""\n    from celerp import config_store as _config_store\n    lock_path = f"{config_path()}.lock"\n    acquired = _config_store._acquire_lock(lock_path)\n    if acquired is None:\n        raise TimeoutError("could not acquire config.toml writer lock")\n    return _config_store, lock_path, acquired\n\n\ndef write_config(cfg: dict) -> None:\n    store, lock_path, (fd, token) = _config_lock()\n    try:\n        _write_config_unlocked(cfg)\n    finally:\n        store._release_lock(fd, lock_path, token)\n\n\ndef _update_cloud_config(mutator):\n    """Locked read-modify-write of [cloud], returning mutator's result."""\n    store, lock_path, (fd, token) = _config_lock()\n    try:\n        cfg = read_config()\n        cloud = cfg.setdefault("cloud", {})\n        result = mutator(cloud)\n        _write_config_unlocked(cfg)\n        return result\n    finally:\n        store._release_lock(fd, lock_path, token)\n\n\ndef resolve_install_order''',
)

# Generate exactly one verifier across API workers. The re-read occurs while the
# cross-process lock is held, so simultaneous send-code requests converge.
regex_once(
    "celerp/config.py",
    r'def ensure_activation_verifier\(\) -> str:.*?\n    return verifier\n\n\ndef activation_challenge',
    '''def ensure_activation_verifier() -> str:\n    """Return one durable verifier shared by every API worker until activation."""\n    if settings.activation_verifier:\n        return settings.activation_verifier\n\n    import secrets as _secrets\n\n    def _ensure(cloud: dict) -> str:\n        stored = cloud.get("activation_verifier")\n        if isinstance(stored, str) and stored:\n            return stored\n        verifier = _secrets.token_urlsafe(32)\n        cloud["activation_verifier"] = verifier\n        return verifier\n\n    verifier = _update_cloud_config(_ensure)\n    settings.activation_verifier = verifier\n    return verifier\n\n\ndef activation_challenge''',
)

# The credential/public-url/verifier transition is one locked disk mutation. The
# verifier is removed only in the same atomic replacement that stores the token.
regex_once(
    "celerp/config.py",
    r'def record_cloud_activation\(.*?\n    settings\.activation_verifier = ""\n\n\ndef persist_cloud_settings',
    '''def record_cloud_activation(\n    gateway_token: str, instance_id: str, *, public_url: str | None = None,\n    tos_version: str | None = None, backup_encryption_key: str | None = None,\n) -> None:\n    """Atomically persist authoritative activation and consume its verifier."""\n    def _record(cloud: dict) -> None:\n        cloud["token"] = gateway_token\n        cloud["instance_id"] = instance_id\n        if public_url:\n            cloud["public_url"] = public_url\n        else:\n            cloud.pop("public_url", None)\n        if tos_version:\n            cloud["tos_version"] = tos_version\n        if backup_encryption_key:\n            cloud["backup_encryption_key"] = backup_encryption_key\n        cloud.pop("disconnected", None)\n        cloud.pop("activation_verifier", None)\n\n    _update_cloud_config(_record)\n    settings.activation_verifier = ""\n\n\ndef persist_cloud_settings''',
)

# Prove simultaneous independent processes all recover the same verifier and the
# resulting TOML is parseable. This exercises the real sidecar lock, not a mock.
p = Path("tests/test_instance_identity.py")
p.write_text(p.read_text() + r'''


def test_activation_verifier_converges_across_processes(tmp_path):
    import os
    import subprocess
    import sys

    cfg = tmp_path / "config.toml"
    env = os.environ.copy()
    env["CELERP_CONFIG"] = str(cfg)
    env["ALLOW_INSECURE_JWT"] = "true"
    code = (
        "from celerp import config; "
        "config.settings.activation_verifier=''; "
        "print(config.ensure_activation_verifier())"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(6)
    ]
    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        outputs.append(out.strip())
    assert all(outputs)
    assert len(set(outputs)) == 1

    import tomllib
    with open(cfg, "rb") as f:
        persisted = tomllib.load(f)
    assert persisted["cloud"]["activation_verifier"] == outputs[0]
''')
