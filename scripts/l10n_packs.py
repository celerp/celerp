# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fetch, check, validate and lock the country packs under celerp/localization.

The packs live in their own repositories and are fetched at build time; this tree commits
only ``packs.lock.json`` (country code to version, schema version and sha256).

    python scripts/l10n_packs.py fetch [root]              # download every locked pack, then check
    python scripts/l10n_packs.py check <root> [--lock F]   # each pack's sha256 equals the lock
    python scripts/l10n_packs.py validate <root>           # each pack's fields (app installed)
    python scripts/l10n_packs.py lock <root>               # validate, then write the lock

``fetch`` reads ``CELERP_L10N_SOURCES`` (JSON ``{"CC": "owner/repo@tag"}``) and
``CELERP_L10N_TOKEN``. It writes nothing unless every pack downloads, and its messages name
the country and HTTP status only, never a repository, URL or tag. ``fetch`` and ``check`` use
the standard library only, so they run under a bare runner Python before any install; only
``validate`` imports the app. A file whose sha256 equals the lock is byte-identical to the
file ``validate`` passed before ``lock`` wrote that digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "celerp" / "localization"
LOCK_NAME = "packs.lock.json"
FORMAT = "celerp-localization"
SCHEMA_VERSION = 1
CONTENTS_URL = "https://api.github.com/repos/{repo}/contents/pack/localization.json?ref={tag}"


def normalized(pack: dict) -> bytes:
    """The one byte form of a pack: two-space JSON, UTF-8, trailing newline."""
    return (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_lock(root: Path) -> dict | None:
    try:
        lock = json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return lock if isinstance(lock, dict) else None


def _pack_files(root: Path) -> dict[str, Path]:
    folder = root / "packs"
    return {p.stem: p for p in sorted(folder.glob("*.json"))} if folder.is_dir() else {}


def check(root: Path, lock_file: Path | None = None) -> list[str]:
    lock = _read_lock(root)
    if lock is None:
        return [f"{LOCK_NAME}: missing or unreadable"]
    files = _pack_files(root)
    defects = []
    for code, entry in lock.items():
        if code not in files:
            defects.append(f"country {code}: pack file missing")
        elif hashlib.sha256(files[code].read_bytes()).hexdigest() != entry.get("sha256"):
            defects.append(f"country {code}: sha256 does not match the lock")
    defects += [f"packs/{code}.json: unexpected file (not in the lock)" for code in files if code not in lock]
    if lock_file is not None and (root / LOCK_NAME).read_bytes() != lock_file.read_bytes():
        defects.append(f"{LOCK_NAME} differs from {lock_file}")
    return defects


def _pack_defects(code: str, data: bytes, locked: dict | None, tax_rate, currency_codes) -> list[str]:
    try:
        pack = json.loads(data)
    except ValueError:
        return [f"country {code}: not valid JSON"]
    if not isinstance(pack, dict):
        return [f"country {code}: not a JSON object"]
    defects = []
    if data != normalized(pack):
        defects.append(f"country {code}: not in normalized form (two-space JSON, UTF-8, trailing newline)")
    if pack.get("format") != FORMAT:
        defects.append(f"country {code}: format is {pack.get('format')!r}, expected {FORMAT!r}")
    if pack.get("schema_version") != SCHEMA_VERSION:
        defects.append(f"country {code}: schema_version is {pack.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    if pack.get("country") != code:
        defects.append(f"country {code}: country field is {pack.get('country')!r}")
    if locked is not None and pack.get("version") != locked.get("version"):
        defects.append(f"country {code}: version {pack.get('version')!r} differs from the lock's {locked.get('version')!r}")
    currency = (pack.get("company_defaults") or {}).get("currency")
    if currency not in currency_codes:
        defects.append(f"country {code}: currency {currency!r} is not a known currency code")
    taxes = pack.get("taxes")
    if not isinstance(taxes, list):
        defects.append(f"country {code}: taxes is not a list")
        taxes = []
    for i, row in enumerate(taxes):
        try:
            tax_rate.model_validate(row)
        except ValueError as exc:  # pydantic's ValidationError
            fields = sorted({str(err["loc"][0]) if err["loc"] else "row" for err in exc.errors()})
            defects.append(f"country {code}: tax row {i} is invalid ({', '.join(fields)})")
    return defects


def validate(root: Path, *, against_lock: bool = True) -> list[str]:
    """Each pack file's fields; with ``against_lock`` its version must equal its lock entry."""
    sys.path.insert(0, str(REPO_ROOT))
    from celerp.routers.companies import TaxRate
    from celerp.services.currencies import CURRENCY_CODES

    lock = _read_lock(root) or {}
    defects = []
    for code, path in _pack_files(root).items():
        locked = lock.get(code) if against_lock else None
        defects += _pack_defects(code, path.read_bytes(), locked, TaxRate, CURRENCY_CODES)
    return defects


def write_lock(root: Path) -> list[str]:
    defects = validate(root, against_lock=False)
    if defects:
        return defects
    files = _pack_files(root)
    previous = [code for code in _read_lock(root) or {} if code in files]
    order = previous + sorted(code for code in files if code not in previous)
    lock = {}
    for code in order:
        data = files[code].read_bytes()
        pack = json.loads(data)
        lock[code] = {"version": pack["version"], "schema_version": pack["schema_version"],
                      "sha256": hashlib.sha256(data).hexdigest()}
    (root / LOCK_NAME).write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return []


def _download(repo: str, tag: str, token: str) -> bytes:
    request = urllib.request.Request(CONTENTS_URL.format(repo=repo, tag=tag), headers={
        "Accept": "application/vnd.github.raw+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "celerp-pack-fetch",
    })
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch(root: Path) -> list[str]:
    token = os.environ.get("CELERP_L10N_TOKEN")
    if not token:
        return ["CELERP_L10N_TOKEN is not set"]
    try:
        sources = json.loads(os.environ.get("CELERP_L10N_SOURCES") or "")
    except ValueError:
        sources = None
    if not isinstance(sources, dict):
        return ["CELERP_L10N_SOURCES is not a JSON object of country code to owner/repo@tag"]
    lock = _read_lock(root)
    if lock is None:
        return [f"{LOCK_NAME}: missing or unreadable"]
    defects = [f"country {code}: in the lock but not in CELERP_L10N_SOURCES" for code in lock if code not in sources]
    defects += [f"country {code}: in CELERP_L10N_SOURCES but not in the lock" for code in sources if code not in lock]
    fetched: dict[str, bytes] = {}
    for code in lock:
        source = sources.get(code)
        if source is None:
            continue
        repo, sep, tag = str(source).partition("@")
        if not sep or repo.count("/") != 1 or not tag:
            defects.append(f"country {code}: source is not owner/repo@tag")
            continue
        try:
            fetched[code] = _download(repo, tag, token)
        except urllib.error.HTTPError as exc:
            defects.append(f"country {code}: fetch failed (HTTP {exc.code})")
        except Exception:  # noqa: BLE001 - the text can carry the URL, which is private
            defects.append(f"country {code}: fetch failed (network error)")
    if defects:
        return defects
    (root / "packs").mkdir(parents=True, exist_ok=True)
    for code, data in fetched.items():
        (root / "packs" / f"{code}.json").write_bytes(data)
    return check(root)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch").add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("root", type=Path)
    check_parser.add_argument("--lock", type=Path)
    commands.add_parser("validate").add_argument("root", type=Path)
    commands.add_parser("lock").add_argument("root", type=Path)
    args = parser.parse_args(argv)
    if args.command == "fetch":
        defects = fetch(args.root)
    elif args.command == "check":
        defects = check(args.root, args.lock)
    elif args.command == "validate":
        defects = validate(args.root)
    else:
        defects = write_lock(args.root)
    for line in defects:
        print(line)
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
