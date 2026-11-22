"""The one validator of the published help bundle (help.json) and of a guide's text.

``bundle_problems`` runs the bundle rules (V1 to V11), then, when a site checkout is
named, that site's own ``website/check_bundle.py``, which builds the site with the
bundle and runs every deploy check over it. ``entry_problems`` holds the limits of
one guide text (title, summary, steps, date); the page format and translations use
the same function, so each limit exists once.

Standard library only and self-contained: the website runs this file alone, read
from the release wheel, with no package installed and an empty environment.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
TOPIC_ID = re.compile(r"^[a-z0-9-]+\.[a-z0-9-]+$")
MODULE_NAME = re.compile(r"^[a-z0-9-]+$")
MAX_TITLE = 120
MAX_STEP_TEXT = 500
MAX_STEPS = 20
MAX_IMAGE_BYTES = 60 * 1024
SITE_CHECK_TIMEOUT = 15 * 60

BUNDLE_KEYS = ("schema", "version", "commit", "site_languages", "locales", "modules",
               "topics", "images", "tombstones")
# A release tag gives X.Y.Z; a development checkout adds a suffix (2.10.1.dev3+g1a2b).
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.+-]+)?$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_LANGUAGE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE = re.compile(r"[<>\x00-\x1f\x7f-\x9f]")


def is_webp(data: bytes) -> bool:
    """A whole WebP file: the RIFF header names WEBP and its size covers exactly the bytes."""
    return (len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") + 8 == len(data))


def _filled(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _real_date(value) -> bool:
    if not (isinstance(value, str) and _DATE.match(value)):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def entry_problems(title, summary, steps, updated) -> list[str]:
    """The problems of one guide text: ``steps`` the step texts, ``updated`` its date
    (None for an English page file). Any JSON value is accepted and never raises."""
    out = []
    if not _filled(title):
        out.append("title must be a non-empty string")
    elif len(title) > MAX_TITLE:
        out.append(f"title is longer than {MAX_TITLE} characters")
    if not _filled(summary):
        out.append("summary must be a non-empty string")
    if not isinstance(steps, list) or not steps:
        out.append("steps must be a non-empty list")
    elif len(steps) > MAX_STEPS:
        out.append(f"a page has at most {MAX_STEPS} steps")
    else:
        for n, text in enumerate(steps, 1):
            if not _filled(text):
                out.append(f"step {n}: text must be a non-empty string")
            elif len(text) > MAX_STEP_TEXT:
                out.append(f"step {n}: text is longer than {MAX_STEP_TEXT} characters")
    if updated is not None and not _real_date(updated):
        out.append("updated must be a real YYYY-MM-DD date")
    return out


def _unique(values) -> bool:
    return len(values) == len(set(values))


def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _object(value, keys) -> bool:
    return isinstance(value, dict) and set(value) == set(keys)


def _strings(value, path):
    """Every string in ``value`` (keys and values) with its path."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}", key
            yield from _strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for n, item in enumerate(value):
            yield from _strings(item, f"{path}[{n}]")


def _languages(b, out):
    site = b["site_languages"]
    if not _str_list(site) or not all(_LANGUAGE.match(c) for c in site) or not _unique(site) \
            or "en" not in site:
        out.append("V3 site_languages: must be unique language codes including \"en\"")
        site = []
    locales = b["locales"]
    if not _str_list(locales) or not _unique(locales) or "en" not in locales:
        out.append("V3 locales: must be unique language codes including \"en\"")
        return None
    for code in locales:
        if code not in site:
            out.append(f"V3 locales: {code!r} is not in site_languages")
    return locales


def _modules(b, out):
    names = []
    if not isinstance(b["modules"], list):
        out.append("V4 modules: must be a list")
        return names
    for n, module in enumerate(b["modules"]):
        where = f"V4 modules[{n}]"
        if not _object(module, ("name", "title")):
            out.append(f"{where}: must be an object with exactly name and title")
        elif not (isinstance(module["name"], str) and MODULE_NAME.match(module["name"])):
            out.append(f"{where}.name: must be lower case letters, digits and -")
        elif module["name"] in names:
            out.append(f"{where}.name: {module['name']!r} is listed twice")
        elif not _filled(module["title"]):
            out.append(f"{where}.title: must be a non-empty string")
        else:
            names.append(module["name"])
    return names


def _text(where, lang, entry, locales, out):
    where = f"{where}.text.{lang}"
    if locales is not None and lang not in locales:
        out.append(f"V6 {where}: {lang!r} is not in locales")
    if not _object(entry, ("title", "summary", "steps", "updated")):
        out.append(f"V6 {where}: must be an object with exactly title, summary, steps and updated")
        return []
    steps = entry["steps"]
    if isinstance(steps, list) and not all(_object(s, ("text", "image")) for s in steps):
        out.append(f"V6 {where}.steps: each step must be an object with exactly text and image")
        return []
    texts = [s["text"] for s in steps] if isinstance(steps, list) else steps
    out += [f"V6 {where}: {p}" for p in entry_problems(entry["title"], entry["summary"], texts,
                                                        entry["updated"])]
    if entry["updated"] is None:
        out.append(f"V6 {where}: updated must be a real YYYY-MM-DD date")
    return steps if isinstance(steps, list) else []


def _topics(b, modules, locales, out):
    """(live ids, {id: related}, [(where, step image)], English titles by topic)."""
    ids, related, images, titles, used = [], {}, [], {}, set()
    if not isinstance(b["topics"], list):
        out.append("V5 topics: must be a list")
        return ids, related, images, titles, used
    for n, topic in enumerate(b["topics"]):
        where = f"topics[{n}]"
        if not _object(topic, ("id", "module", "order", "related", "text")):
            out.append(f"V5 {where}: must be an object with exactly id, module, order, related and text")
            continue
        tid = topic["id"]
        if not (isinstance(tid, str) and TOPIC_ID.match(tid)):
            out.append(f"V5 {where}.id: must look like <module>.<page>")
            continue
        where = f"topics[{tid}]"
        if tid in ids:
            out.append(f"V5 {where}: id is listed twice")
            continue
        ids.append(tid)
        if topic["module"] not in modules or tid.split(".", 1)[0] != topic["module"]:
            out.append(f"V5 {where}.module: must be a listed module and the id's prefix")
        else:
            used.add(topic["module"])
        if not isinstance(topic["order"], int) or isinstance(topic["order"], bool):
            out.append(f"V5 {where}.order: must be a whole number")
        related[tid] = topic["related"]
        text = topic["text"]
        if not isinstance(text, dict) or "en" not in text:
            out.append(f"V6 {where}.text: must be an object with an \"en\" entry")
            continue
        for lang, entry in text.items():
            for k, step in enumerate(_text(where, lang, entry, locales, out), 1):
                images.append((f"{where}.text.{lang}.steps[{k}]", step["image"]))
        if isinstance(text["en"], dict) and _filled(text["en"].get("title")):
            titles[tid] = " ".join(text["en"]["title"].lower().split())
    return ids, related, images, titles, used


def _images(b, steps, out):
    store = b["images"]
    if not isinstance(store, dict):
        out.append("V9 images: must be an object")
        store = {}
    for key, data in store.items():
        try:
            raw = base64.b64decode(data, validate=True) if isinstance(data, str) else None
        except (binascii.Error, ValueError):
            raw = None
        if raw is None or not is_webp(raw):
            out.append(f"V9 images.{key}: must be base64 of a WebP image")
        elif len(raw) > MAX_IMAGE_BYTES:
            out.append(f"V9 images.{key}: is larger than {MAX_IMAGE_BYTES // 1024} KB")
        elif not _SHA.match(key) or hashlib.sha256(raw).hexdigest() != key:
            out.append(f"V9 images.{key}: the key must be the sha256 of the image")
    for where, image in steps:
        if image is not None and (not isinstance(image, str) or image not in store):
            out.append(f"V9 {where}.image: must be null or a key of images")


def _tombstones(b, ids, modules, out):
    if not isinstance(b["tombstones"], list):
        out.append("V10 tombstones: must be a list")
        return
    seen = set()
    for n, stone in enumerate(b["tombstones"]):
        where = f"V10 tombstones[{n}]"
        if not _object(stone, ("id", "kind", "replaced_by")) or stone["kind"] not in ("topic", "module"):
            out.append(f"{where}: must be an object with id, kind (topic or module) and replaced_by")
            continue
        sid, pattern = stone["id"], TOPIC_ID if stone["kind"] == "topic" else MODULE_NAME
        if not (isinstance(sid, str) and pattern.match(sid)):
            out.append(f"{where}.id: is not a {stone['kind']} id")
        elif sid in seen:
            out.append(f"{where}.id: {sid!r} is listed twice")
        elif sid in ids or sid in modules:
            out.append(f"{where}.id: {sid!r} is live")
        else:
            seen.add(sid)
        target = stone["replaced_by"]
        if not (target == "" or target in ids or target in modules):
            out.append(f"{where}.replaced_by: must be a live topic id, a live module or \"\"")


def _table_v(b) -> list[str]:
    if not isinstance(b, dict) or set(b) != set(BUNDLE_KEYS):
        return ["V1 bundle: must be an object with exactly the keys " + ", ".join(BUNDLE_KEYS)]
    out = []
    if b["schema"] != SCHEMA or isinstance(b["schema"], bool):
        out.append(f"V1 schema: must be {SCHEMA}")
    if not (isinstance(b["version"], str) and _VERSION.match(b["version"])):
        out.append("V2 version: must be a release version like 2.10.0")
    if not (isinstance(b["commit"], str) and _COMMIT.match(b["commit"])):
        out.append("V2 commit: must be 40 lower case hex digits")
    locales = _languages(b, out)
    modules = _modules(b, out)
    ids, related, steps, titles, used = _topics(b, modules, locales, out)
    out += [f"V4 modules: {m!r} has no topic" for m in modules if m not in used]
    by_title = {}
    for tid, title in titles.items():
        if title in by_title:
            out.append(f"V7 topics[{tid}].text.en.title: same title as {by_title[title]}")
        by_title.setdefault(title, tid)
    for tid, refs in related.items():
        if not _str_list(refs) or any(r == tid or r not in ids for r in refs):
            out.append(f"V8 topics[{tid}].related: must list other live topic ids")
    _images(b, steps, out)
    _tombstones(b, ids, modules, out)
    out += [f"V11 {where}: has <, > or a control character"
            for where, text in _strings({k: b[k] for k in BUNDLE_KEYS if k != "images"}, "bundle")
            if _UNSAFE.search(text)]
    return out


def _site_problems(b, site) -> list[str]:
    check = Path(site) / "website" / "check_bundle.py"
    if not check.is_file():
        return [f"IF14: no site checkout at {site} (website/check_bundle.py is missing)"]
    with tempfile.TemporaryDirectory(prefix="help-bundle-") as tmp:
        path = Path(tmp) / "help.json"
        path.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        try:
            run = subprocess.run([sys.executable, str(check), str(path)], capture_output=True,
                                 text=True, timeout=SITE_CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return [f"IF14: {check} ran past {SITE_CHECK_TIMEOUT} seconds"]
        except OSError as e:
            return [f"IF14: {check} could not start: {e}"]
    if run.returncode == 0:
        return []
    lines = [line for line in (run.stdout + run.stderr).splitlines() if line.strip()]
    return [f"IF14 {line}" for line in lines] or [f"IF14: {check} exited {run.returncode}"]


def bundle_problems(bundle, site=None) -> list[str]:
    """Every problem of a parsed help bundle, empty when it is valid: the V rules, then,
    when they pass and a site checkout is named (``site``, else env CELERP_SITE), the
    problems that site's check_bundle.py prints. Never raises."""
    try:
        problems = _table_v(bundle)
    except Exception as e:  # noqa: BLE001  a validator crash is a named problem, never a pass
        return [f"V1 bundle: the validator failed: {type(e).__name__}: {e}"]
    site = site or os.environ.get("CELERP_SITE")
    if problems or not site:
        return problems
    return _site_problems(bundle, site)
