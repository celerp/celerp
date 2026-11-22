# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Export guide prose for translation and apply the translated file back.

    python scripts/help_translations.py todo --langs th,es --out todo.json
    python scripts/help_translations.py apply todo.json

``todo`` writes every guide whose translation in a named language is missing or no
longer matches the English: the English text, its hash, and an empty slot per language.
``apply`` takes that file with slots filled (a slot left empty is skipped), checks the
whole file before writing anything, then writes the changed catalogs and the ledger last,
so after a failure part-way the ledger still lacks the new language and a re-run finishes
the job.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from celerp.help import ledger, translations
from celerp.help.assemble import build_bundle
from celerp.help.bundle import bundle_problems
from celerp.help.checks import shipped_pages

SLOT_KEYS = ("title", "summary", "steps")


class Refused(Exception):
    """The run stops with these problems and writes nothing more."""


def _english(data: dict) -> dict:
    return {"title": data["title"], "summary": data["summary"], "steps": [s["text"] for s in data["steps"]]}


def _pages() -> dict:
    return {p.data["id"]: p.data for p in shipped_pages() if isinstance(p.data, dict)}


def _languages(codes) -> list[str]:
    bad = [c for c in codes if not isinstance(c, str) or c == "en" or c not in ledger.SITE_LANGUAGES]
    if bad or not codes:
        raise Refused([f"{c!r} is not a site language other than en" for c in bad] or ["no language named"])
    return list(codes)


def _catalog(lang: str) -> dict:
    try:
        return translations.catalog(lang)
    except ValueError as e:
        raise Refused([str(e)]) from e


def todo(langs: list[str], out: str) -> None:
    langs = _languages(langs)
    catalogs = {lang: _catalog(lang) for lang in langs}
    entries = {}
    for pid, data in sorted(_pages().items()):
        english = _english(data)
        empty = {"title": "", "summary": "", "steps": [""] * len(english["steps"])}
        slots = {lang: dict(empty, steps=list(empty["steps"])) for lang in langs
                 if translations.entry_state(data, catalogs[lang].get(pid)) != ("fresh", [])}
        if slots:
            entries[pid] = {"source": translations.source_hash(data), "en": english, **slots}
    try:
        ledger.write_data(Path(out), {"languages": langs, "entries": entries})
    except OSError as e:
        raise Refused([f"{out}: cannot be written: {e}"]) from e


def _filled(slot) -> bool:
    """A slot with any field set (anything not a slot counts, so it is refused by name)."""
    if not isinstance(slot, dict):
        return True
    steps = slot.get("steps")
    values = [v for k, v in slot.items() if k != "steps"] + (steps if isinstance(steps, list) else [steps])
    return any(v not in ("", None) for v in values)


def _stamped(doc, pages: dict, today: str) -> dict:
    """{lang: {page_id: entry}} for every filled slot, after checking the whole file."""
    if not (isinstance(doc, dict) and isinstance(doc.get("entries"), dict)):
        raise Refused(["the file must be an object with languages and entries"])
    langs = _languages(doc.get("languages") if isinstance(doc.get("languages"), list) else [])
    out, problems = {}, []
    for pid, item in doc["entries"].items():
        if pid not in pages:
            problems.append(f"{pid}: is not a live page id")
            continue
        if not isinstance(item, dict):
            problems.append(f"{pid}: must be an object")
            continue
        source = translations.source_hash(pages[pid])
        slots = {lang: item[lang] for lang in langs if lang in item and _filled(item[lang])}
        if slots and item.get("source") != source:
            problems.append(f"{pid}: the English changed since this file was exported: export it again")
            continue
        for lang, slot in slots.items():
            if not (isinstance(slot, dict) and set(slot) == set(SLOT_KEYS)):
                problems.append(f"{pid} {lang}: must be an object with exactly " + ", ".join(SLOT_KEYS))
                continue
            entry = {"source": source, **slot, "updated": today}
            problems += [f"{pid} {lang}: {p}" for p in translations.entry_state(pages[pid], entry)[1]]
            out.setdefault(lang, {})[pid] = entry
    if problems:
        raise Refused(problems)
    if out:
        try:
            bundle = build_bundle(lambda path: today, overlay=out)
        except ValueError as e:
            raise Refused([str(e)]) from e
        problems = bundle_problems(bundle)
        if problems:
            raise Refused(problems)
    return out


def _same_text(a, b) -> bool:
    return isinstance(a, dict) and isinstance(b, dict) and \
        {k: v for k, v in a.items() if k != "updated"} == {k: v for k, v in b.items() if k != "updated"}


def apply(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise Refused([f"{path}: is not a readable JSON file: {e}"]) from e
    overlay = _stamped(doc, _pages(), date.today().isoformat())
    for lang in sorted(overlay):
        stored = _catalog(lang)
        merged = {**stored, **{pid: stored[pid] if _same_text(stored.get(pid), entry) else entry
                               for pid, entry in overlay[lang].items()}}
        if merged != stored:
            try:
                translations.write_catalog(lang, merged)
            except OSError as e:
                raise Refused([f"{lang}.json: cannot be written: {e}"]) from e
    try:
        data = ledger.ledger()
    except ValueError as e:
        raise Refused([str(e)]) from e
    new = [lang for lang in overlay if lang not in data["locales"]]
    if new:
        try:
            ledger.write_ledger({**data, "locales": [*data["locales"], *new]})
        except OSError as e:
            raise Refused([f"{ledger.LEDGER_FILE.name}: cannot be written: {e}"]) from e


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("todo", help="write the guides that need translating")
    export.add_argument("--langs", required=True, help="comma-separated language codes")
    export.add_argument("--out", required=True, help="the file to write")
    sub.add_parser("apply", help="write the translations in a filled todo file").add_argument("file")
    args = parser.parse_args(argv)
    try:
        if args.command == "todo":
            todo(args.langs.split(","), args.out)
        else:
            apply(args.file)
    except Refused as e:
        for problem in e.args[0]:
            print(problem, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
