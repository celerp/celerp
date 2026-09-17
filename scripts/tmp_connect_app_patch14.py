from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    if text.count(old) != 1:
        raise RuntimeError(f"{path}: expected one match, got {text.count(old)}")
    p.write_text(text.replace(old, new))


# The sidecar lock is the first filesystem write on a fresh installation. Ensure
# its parent exists before O_EXCL creates the lock, just as the serializer already
# ensures the config parent exists before writing config.toml.
replace_once(
    "celerp/config.py",
    '''def _config_lock():\n    """Acquire the cross-process config.toml writer lock or fail closed."""\n    from celerp import config_store as _config_store\n    lock_path = f"{config_path()}.lock"\n''',
    '''def _config_lock():\n    """Acquire the cross-process config.toml writer lock or fail closed."""\n    from celerp import config_store as _config_store\n    path = config_path()\n    path.parent.mkdir(parents=True, exist_ok=True)\n    lock_path = f"{path}.lock"\n''',
)
