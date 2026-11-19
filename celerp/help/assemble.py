"""Assemble the published help bundle (help.json, schema 1) from the core and
first-party guides, their translations and the ledger. The result is not validated
here: callers pass it to bundle_problems."""
from __future__ import annotations

import base64
import hashlib
import subprocess
from pathlib import Path

from celerp import __version__
from celerp.help.bundle import SCHEMA
from celerp.help.checks import shipped_pages
from celerp.help.ledger import SITE_LANGUAGES, ledger
from celerp.help.registry import CORE, get_index
from celerp.help.render import render_step
from celerp.help.schema import module_prefix, read_image
from celerp.help.translations import catalog, entry_state

_REPO = Path(__file__).resolve().parent.parent.parent


def _git(*args: str) -> str:
    run = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, text=True)
    if run.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {run.stderr.strip()}")
    return run.stdout.strip()


def git_date(path) -> str:
    """The date (YYYY-MM-DD) of the last commit that changed ``path``. Raises on a
    shallow clone, whose history would give a wrong date, or a file with no commit."""
    if _git("rev-parse", "--is-shallow-repository") != "false":
        raise RuntimeError("the checkout is shallow: fetch the full history for guide dates")
    date = _git("log", "-1", "--format=%cs", "--", str(path))
    if not date:
        raise RuntimeError(f"{path} has no commit: commit it before exporting help")
    return date


def build_bundle(dates, overlay=None) -> dict:
    """The help bundle: ``dates(file)`` gives each English page file's date; ``overlay``
    ({lang: {page_id: entry}}) replaces stored catalog entries."""
    overlay = overlay or {}
    led = ledger()
    files = {p.data.get("id"): p for p in shipped_pages() if isinstance(p.data, dict)}
    pages = sorted((p for p in get_index().pages.values() if p.source in ("core", "first_party")),
                   key=lambda p: p.id)
    locales = [*led["locales"], *(lang for lang in overlay if lang not in led["locales"])]
    catalogs = {lang: {**catalog(lang), **overlay.get(lang, {})} for lang in locales if lang != "en"}
    exported = {p.id for p in pages}
    images, topics, modules = {}, [], {}
    for page in pages:
        data = files[page.id].data
        shas = []
        for n in range(1, len(page.steps) + 1):
            raw = read_image(page, n)
            sha = hashlib.sha256(raw).hexdigest() if raw else None
            if sha:
                images[sha] = base64.b64encode(raw).decode("ascii")
            shas.append(sha)

        def steps(texts, lang):
            return [{"text": render_step({**step, "text": text}, lang, page.catalogs), "image": sha}
                    for step, text, sha in zip(page.steps, texts, shas)]

        text = {"en": {"title": page.title, "summary": page.summary,
                       "steps": steps([s["text"] for s in page.steps], "en"),
                       "updated": dates(files[page.id].path)}}
        for lang, entries in catalogs.items():
            entry = entries.get(page.id)
            if entry_state(data, entry)[0] == "fresh":
                text[lang] = {"title": entry["title"], "summary": entry["summary"],
                              "steps": steps(entry["steps"], lang), "updated": entry["updated"]}
        name = module_prefix(page.module)
        modules[name] = page.module_display
        topics.append({"id": page.id, "module": name, "order": page.order,
                       "related": [r for r in page.related if r in exported], "text": text})
    order = sorted(modules, key=lambda m: (m != CORE, modules[m].casefold(), m))
    return {
        "schema": SCHEMA, "version": __version__, "commit": _git("rev-parse", "HEAD"),
        "site_languages": list(SITE_LANGUAGES), "locales": locales,
        "modules": [{"name": m, "title": modules[m]} for m in order],
        "topics": topics, "images": images,
        "tombstones": [*({"id": tid, "kind": "topic", "replaced_by": target}
                         for tid, target in sorted(led["topics"].items()) if target is not None),
                       *({"id": name, "kind": "module", "replaced_by": target}
                         for name, target in sorted(led["modules"].items()))],
    }
