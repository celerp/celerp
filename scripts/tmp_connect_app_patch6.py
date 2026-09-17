from pathlib import Path
import re


def regex_once(path: str, pattern: str, replacement: str) -> None:
    p = Path(path)
    text = p.read_text()
    new, n = re.subn(pattern, replacement, text, count=1, flags=re.S)
    if n != 1:
        raise RuntimeError(f"{path}: regex matched {n} times")
    p.write_text(new)


# A fresh multi-worker server must elect one durable instance id just as it elects
# one activation verifier. Both operations use the same locked read-modify-write.
regex_once(
    "celerp/config.py",
    r'def ensure_instance_id\(\) -> str:.*?\n\ndef ensure_activation_verifier',
    '''def ensure_instance_id() -> str:\n    """Return one durable instance id shared by every local process."""\n    if settings.gateway_instance_id:\n        return settings.gateway_instance_id\n\n    import uuid as _uuid\n\n    def _ensure(cloud: dict) -> str:\n        stored = cloud.get("instance_id")\n        if isinstance(stored, str) and stored:\n            return stored\n        iid = str(_uuid.uuid4())\n        cloud["instance_id"] = iid\n        return iid\n\n    iid = _update_cloud_config(_ensure)\n    settings.gateway_instance_id = iid\n    return iid\n\n\ndef ensure_activation_verifier''',
)

# Prove independent processes starting from an empty config all see the same
# machine identity and the same proof verifier, with a parseable durable file.
p = Path("tests/test_instance_identity.py")
p.write_text(p.read_text() + r'''


def test_instance_identity_and_verifier_converge_across_processes(tmp_path):
    import os
    import subprocess
    import sys

    cfg = tmp_path / "fresh-config.toml"
    env = os.environ.copy()
    env["CELERP_CONFIG"] = str(cfg)
    env["ALLOW_INSECURE_JWT"] = "true"
    code = (
        "from celerp import config; "
        "config.settings.gateway_instance_id=''; "
        "config.settings.activation_verifier=''; "
        "print(config.ensure_instance_id(), config.ensure_activation_verifier())"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env)
        for _ in range(6)
    ]
    values = []
    for proc in procs:
        out, err = proc.communicate(timeout=20)
        assert proc.returncode == 0, err
        values.append(tuple(out.strip().split()))
    assert all(len(v) == 2 for v in values)
    assert len({v[0] for v in values}) == 1
    assert len({v[1] for v in values}) == 1

    import tomllib
    with open(cfg, "rb") as f:
        persisted = tomllib.load(f)["cloud"]
    assert persisted["instance_id"] == values[0][0]
    assert persisted["activation_verifier"] == values[0][1]
''')
