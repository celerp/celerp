"""The help ledger (ledger.json beside this file): every core and first-party guide id
ever released and what replaced it, retired modules, and every guide language ever
released. Only this module reads or writes the file.

``{"locales": ["en", ...], "topics": {id: null | replacement}, "modules": {retired: replacement}}``:
a null topic is live; a replacement is a topic id, a module name or "" (the help index).
"""
from __future__ import annotations

import json
from pathlib import Path

from celerp.config_store import atomic_write_text

LEDGER_FILE = Path(__file__).resolve().parent / "ledger.json"

# A copy of the website's LANGUAGES: the languages a guide can be published in.
SITE_LANGUAGES = ("en", "th", "es", "pt", "fr", "vi", "ja", "de", "it", "id", "ar", "am")


def read_data(path: Path, missing):
    """The parsed JSON object in ``path``, or ``missing`` when there is no file.
    Raises ValueError naming a file that is not a JSON object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return missing
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"{path.name} is not readable: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path.name} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must hold a JSON object")
    return data


def write_data(path: Path, data) -> None:
    """Replace ``path`` with ``data`` atomically; raises OSError with the old file kept."""
    atomic_write_text(str(path), json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def ledger() -> dict:
    data = read_data(LEDGER_FILE, None)
    if data is None:
        raise ValueError(f"{LEDGER_FILE.name} is missing")
    return data


def write_ledger(data: dict) -> None:
    write_data(LEDGER_FILE, data)
