# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fetch, check, validate and lock the country packs under celerp/localization.

The packs live in their own repositories and are fetched at build time; this tree commits
only ``packs.lock.json`` (country code to version, schema version and sha256).

    python scripts/l10n_packs.py fetch [root]              # download every locked pack, then check
    python scripts/l10n_packs.py check <root> [--lock F]   # each pack's sha256 equals the lock
    python scripts/l10n_packs.py validate <root>           # each pack's fields (app installed)
    python scripts/l10n_packs.py lock <root> [--add CC] [--remove CC]
                                                           # validate, then write the lock

``fetch`` downloads each locked country's pack from ``celerp/celerp-l10n-<cc>`` at tag
``v<version>``, both taken from the lock, with ``CELERP_L10N_TOKEN``. It replaces the packs folder
only after every pack downloads and matches its locked sha256 (any failure leaves the old files
as they were), sends the token to the first host only (never across a redirect to another host),
and its messages name the country and HTTP status. ``fetch`` and ``check`` use
the standard library only, so they run under a bare runner Python before any install; only
``validate`` imports the app. A file whose sha256 equals the lock is byte-identical to the
file ``validate`` passed before ``lock`` wrote that digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = REPO_ROOT / "celerp" / "localization"
LOCK_NAME = "packs.lock.json"
# A lock key: an ISO country code, or zz and a code for a test pack; the same pattern as
# COUNTRY_CODE in celerp/localization (this script's fetch and check import nothing from the app).
COUNTRY_CODE = r"(zz)?[A-Z]{2}"
_ROW_NOUN = {"taxes": "tax", "retired": "retired", "item_rate_table": "item_rate_table"}
_ACCOUNT_FIELDS = ("output_account", "input_account")
PACK_REPO = "celerp/celerp-l10n-{cc}"
CONTENTS_URL = "https://api.github.com/repos/{repo}/contents/pack/localization.json?ref={tag}"


def normalized(pack: dict) -> bytes:
    """The one byte form of a pack: two-space JSON, UTF-8, trailing newline."""
    return (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_lock(root: Path) -> dict | None:
    try:
        lock = json.loads((root / LOCK_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(lock, dict):
        return None
    return lock if all(isinstance(code, str) and re.fullmatch(COUNTRY_CODE, code) and isinstance(e, dict)
                       and isinstance(e.get("sha256"), str) and isinstance(e.get("version"), str)
                       for code, e in lock.items()) else None


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


def _pack_defects(code: str, data: bytes, locked: dict | None, pack_schema) -> list[str]:
    try:
        pack = json.loads(data)
    except ValueError:
        return [f"country {code}: not valid JSON"]
    if not isinstance(pack, dict):
        return [f"country {code}: not a JSON object"]
    defects = []
    if data != normalized(pack):
        defects.append(f"country {code}: not in normalized form (two-space JSON, UTF-8, trailing newline)")
    if pack.get("country") != code:
        defects.append(f"country {code}: country field is {pack.get('country')!r}")
    if locked is not None and pack.get("version") != locked.get("version"):
        defects.append(f"country {code}: version {pack.get('version')!r} differs from the lock's {locked.get('version')!r}")
    try:
        pack_schema.model_validate_json(data)
    except ValueError as exc:  # pydantic's ValidationError
        rows: dict[tuple[str, int], set[str]] = {}
        for err in exc.errors():
            loc = err["loc"]
            if len(loc) > 2 and loc[0] in _ROW_NOUN and isinstance(loc[1], int):
                rows.setdefault((loc[0], loc[1]), set()).add(str(loc[2]))
            elif len(loc) == 2 and loc[0] in _ROW_NOUN and isinstance(loc[1], int):
                defects.append(f"country {code}: {_ROW_NOUN[loc[0]]} row {loc[1]}: {err['msg'].removeprefix('Value error, ')}")
            else:
                where = ".".join(str(part) for part in loc) or "pack"
                defects.append(f"country {code}: {where}: {err['msg'].removeprefix('Value error, ')}")
        defects += [f"country {code}: {_ROW_NOUN[field]} row {i} is invalid ({', '.join(sorted(fields))})"
                    for (field, i), fields in sorted(rows.items())]
    else:
        chart = _default_chart()
        defects += [f"country {code}: tax row {i}: {field} {tax[field]} is not an account of the default chart"
                    for i, tax in enumerate(pack["taxes"]) for field in _ACCOUNT_FIELDS
                    if field in tax and tax[field] not in chart]
    return defects


def _default_chart() -> frozenset[str]:
    """The account codes of the chart Celerp seeds a company with."""
    sys.path.insert(0, str(REPO_ROOT / "default_modules" / "celerp-accounting"))
    from celerp_accounting.routes import DEFAULT_CHART_OF_ACCOUNTS
    return frozenset(entry["code"] for entry in DEFAULT_CHART_OF_ACCOUNTS)


def validate(root: Path, *, against_lock: bool = True) -> list[str]:
    """Each pack file's fields (the CountryPack schema) and the accounts its taxes name (the
    default chart); with ``against_lock`` its version must equal its lock entry."""
    sys.path.insert(0, str(REPO_ROOT))
    from celerp.routers.companies import CountryPack

    lock = _read_lock(root) or {}
    defects = []
    for code, path in _pack_files(root).items():
        locked = lock.get(code) if against_lock else None
        defects += _pack_defects(code, path.read_bytes(), locked, CountryPack)
    return defects


def write_lock(root: Path, add: list[str] | tuple[str, ...] = (), remove: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Validate, then write the lock for the locked country set, plus ``add``, minus ``remove``.

    The set changes only by those two: a locked country whose pack file is missing fails (an
    unfetched or partial checkout never shrinks the lock), and so does a pack file outside
    the set. With no lock file yet, the set starts empty."""
    path = root / LOCK_NAME
    lock = _read_lock(root) if path.exists() else {}
    if lock is None:
        return [f"{LOCK_NAME}: unreadable"]
    defects = [f"country {code}: already in the lock" for code in add if code in lock]
    defects += [f"country {code}: not in the lock" for code in remove if code not in lock]
    if defects:
        return defects
    order = [code for code in lock if code not in remove] + list(dict.fromkeys(add))
    files = _pack_files(root)
    defects = [f"country {code}: pack file missing (pass --remove {code} to take it out of the lock)"
               for code in order if code not in files]
    defects += [f"packs/{code}.json: not in the lock (pass --add {code} to lock it)" for code in files if code not in order]
    defects = defects or validate(root, against_lock=False)
    if defects:
        return defects
    new = {}
    for code in order:
        data = files[code].read_bytes()
        pack = json.loads(data)
        new[code] = {"version": pack["version"], "schema_version": pack["schema_version"],
                     "sha256": hashlib.sha256(data).hexdigest()}
    path.write_text(json.dumps(new, indent=2) + "\n", encoding="utf-8")
    return []


class _SameHostAuthorization(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but send the token only to the scheme, host and port it was meant for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(new.full_url) != _origin(req.full_url):
            new.remove_header("Authorization")
        return new


def _origin(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


def _download(repo: str, tag: str, token: str) -> bytes:
    url = CONTENTS_URL.format(repo=repo, tag=urllib.parse.quote(tag, safe=""))
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github.raw+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "celerp-pack-fetch",
    })
    with urllib.request.build_opener(_SameHostAuthorization).open(request, timeout=60) as response:
        return response.read()


def _replace_packs(root: Path, fetched: dict[str, bytes]) -> None:
    """Write the set into a new folder beside ``packs``, then swap it in; on any failure the old
    one stays. The old set is kept in ``.packs-old`` until the new set is in place or the old one
    is back: when neither move succeeds it stays there and the error names it."""
    packs, old = root / "packs", root / ".packs-old"
    staging = Path(tempfile.mkdtemp(prefix=".packs-new-", dir=root))
    try:
        for code, data in fetched.items():
            (staging / f"{code}.json").write_bytes(data)
        if old.exists():
            if not packs.exists():
                raise OSError(f"{old} holds the last pack set and {packs} is missing; move it back first")
            shutil.rmtree(old)
        moved = packs.exists()
        if moved:
            os.replace(packs, old)
        try:
            os.replace(staging, packs)
        except BaseException:
            if moved:
                try:
                    os.replace(old, packs)
                except OSError as exc:
                    raise OSError(f"the old pack set could not be moved back and is kept in {old}") from exc
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def fetch(root: Path) -> list[str]:
    token = os.environ.get("CELERP_L10N_TOKEN")
    if not token:
        return ["CELERP_L10N_TOKEN is not set"]
    lock = _read_lock(root)
    if lock is None:
        return [f"{LOCK_NAME}: missing or unreadable"]
    defects: list[str] = []
    fetched: dict[str, bytes] = {}
    for code, entry in lock.items():
        try:
            fetched[code] = _download(PACK_REPO.format(cc=code.lower()), f"v{entry['version']}", token)
        except urllib.error.HTTPError as exc:
            defects.append(f"country {code}: fetch failed (HTTP {exc.code})")
        except Exception:  # noqa: BLE001 - messages name the country only, never the URL
            defects.append(f"country {code}: fetch failed (network error)")
    defects += [f"country {code}: sha256 does not match the lock" for code, data in fetched.items()
                if hashlib.sha256(data).hexdigest() != lock[code]["sha256"]]
    if defects:
        return defects
    try:
        _replace_packs(root, fetched)
    except OSError as exc:
        return [f"packs: not replaced ({exc.strerror or exc})"]
    return check(root)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch").add_argument("root", nargs="?", type=Path, default=DEFAULT_ROOT)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("root", type=Path)
    check_parser.add_argument("--lock", type=Path)
    commands.add_parser("validate").add_argument("root", type=Path)
    lock_parser = commands.add_parser("lock")
    lock_parser.add_argument("root", type=Path)
    lock_parser.add_argument("--add", action="append", default=[], metavar="CC", help="lock a new country's pack")
    lock_parser.add_argument("--remove", action="append", default=[], metavar="CC", help="take a country out of the lock")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        defects = fetch(args.root)
    elif args.command == "check":
        defects = check(args.root, args.lock)
    elif args.command == "validate":
        defects = validate(args.root)
    else:
        defects = write_lock(args.root, args.add, args.remove)
    for line in defects:
        print(line)
    return 1 if defects else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
