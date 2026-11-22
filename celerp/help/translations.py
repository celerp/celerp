"""Translations of guide prose: one catalog per language, translations/<lang>.json
beside this file, ``{page_id: {source, title, summary, steps, updated}}``. An entry is
fresh while ``source`` is the hash of the English it was translated from."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from celerp.help.bundle import entry_problems
from celerp.help.ledger import read_data, write_data
from celerp.help.schema import PLACEHOLDER

TRANSLATIONS_DIR = Path(__file__).resolve().parent / "translations"
ENTRY_KEYS = ("source", "title", "summary", "steps", "updated")


def source_hash(page_data: dict) -> str:
    """The sha256 of a page's English title, summary and step texts."""
    english = [page_data["title"], page_data["summary"], [s["text"] for s in page_data["steps"]]]
    return hashlib.sha256(json.dumps(english, ensure_ascii=False).encode("utf-8")).hexdigest()


def entry_state(page_data: dict, entry) -> tuple[str, list[str]]:
    """("absent" | "stale" | "fresh", problems) of one catalog entry against the page."""
    if entry is None:
        return "absent", []
    if not isinstance(entry, dict) or set(entry) != set(ENTRY_KEYS):
        return "stale", ["must be an object with exactly " + ", ".join(ENTRY_KEYS)]
    if entry["source"] != source_hash(page_data):
        return "stale", []
    problems = entry_problems(entry["title"], entry["summary"], entry["steps"], entry["updated"])
    if entry["updated"] is None:
        problems.append("updated must be a real YYYY-MM-DD date")
    english = [s["text"] for s in page_data["steps"]]
    steps = entry["steps"]
    if isinstance(steps, list) and len(steps) != len(english):
        problems.append(f"has {len(steps)} steps, the English has {len(english)}")
    elif isinstance(steps, list):
        problems += [f"step {n}: placeholders differ from the English"
                     for n, (ours, theirs) in enumerate(zip(steps, english), 1)
                     if isinstance(ours, str) and sorted(PLACEHOLDER.findall(ours)) != sorted(PLACEHOLDER.findall(theirs))]
    return "fresh", problems


def _path(lang: str) -> Path:
    return TRANSLATIONS_DIR / f"{lang}.json"


def catalog(lang: str) -> dict:
    """The catalog of ``lang`` ({} with no file); ValueError names a non-JSON file."""
    return read_data(_path(lang), {})


def catalog_languages() -> list[str]:
    """The languages that have a catalog file."""
    return sorted(p.stem for p in TRANSLATIONS_DIR.glob("*.json"))


def write_catalog(lang: str, entries: dict) -> None:
    TRANSLATIONS_DIR.mkdir(exist_ok=True)
    write_data(_path(lang), entries)
