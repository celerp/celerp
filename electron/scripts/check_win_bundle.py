#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Inspect a packaged Windows build: what it contains, and whether it is complete.

  carve <Celerp-Setup.exe> <out.7z>   copy the application archive out of an installer
  inventory <win-unpacked> [--json F] file count, bytes and per-root totals, app.asar included
  check <win-unpacked>                the packaged-tree assertions below; exit 1 on any failure
  installed <win-unpacked> <folder>   an installed folder holds exactly the build's files, each
                                      with the build's SHA-256, and the uninstaller; exit 1
                                      otherwise

`check` reads the final tree, including the app.asar index, and reports every
assertion. Those marked "removed" are what the Windows build now leaves out;
the others hold for every release and guard against pruning too much.
Standard library only, so it runs on the build runner and on any machine
holding an extracted installer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import re
import struct
import sys
from pathlib import Path

PRUNE = json.loads((Path(__file__).resolve().parent.parent / "win-prune.json").read_text("utf-8"))
SEVEN_ZIP_SIGNATURE = b"7z\xbc\xaf\x27\x1c"
PG = "resources/app.asar.unpacked/node_modules/@embedded-postgres"
PG_PROGRAMS = (
    f"{PG}/windows-x64/native/bin/postgres.exe",
    f"{PG}/windows-x64/native/bin/initdb.exe",
    f"{PG}/windows-x64/native/bin/pg_ctl.exe",
    "resources/pg-win/bin/pg_dump.exe",
    "resources/pg-win/bin/pg_restore.exe",
)
LICENCES = (
    "resources/licenses/THIRD-PARTY-NOTICES.txt",
    "resources/licenses/chromium/LICENSES.chromium.html",
    "resources/licenses/electron/LICENSE",
    "resources/licenses/python/LICENSE.openssl-3.txt",
    "resources/licenses/psycopg2/LGPL-3.0.txt",
    "resources/licenses/embedded-postgres/zlib-LICENSE.txt",
)


def carve(setup: Path, out: Path) -> int:
    """The NSIS installer embeds the app as one 7z archive; its start header
    gives the archive's full length (32 + next-header offset + size)."""
    with setup.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
        at = m.find(SEVEN_ZIP_SIGNATURE)
        if at < 0:
            print(f"{setup}: no 7z archive inside", file=sys.stderr)
            return 1
        offset, size = struct.unpack_from("<QQ", m, at + 12)
        out.write_bytes(m[at:at + 32 + offset + size])
    print(f"{out}: {32 + offset + size} bytes from offset {at}")
    return 0


def asar_entries(asar: Path) -> dict[str, dict]:
    """Every file in an asar archive, keyed by its path inside the archive."""
    with asar.open("rb") as f:
        head = f.read(16)
        json_len = struct.unpack_from("<I", head, 12)[0]
        index = json.loads(f.read(json_len))
    found: dict[str, dict] = {}

    def walk(node: dict, prefix: str) -> None:
        for name, child in node.get("files", {}).items():
            path = f"{prefix}{name}"
            if "files" in child:
                walk(child, path + "/")
            else:
                found[path] = child
    walk(index, "")
    return found


def inventory(root: Path) -> dict:
    roots: dict[str, list[int]] = {}
    files = total = 0
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        size = p.stat().st_size
        files += 1
        total += size
        rel = p.relative_to(root).parts
        key = "/".join(rel[:3]) if rel[0] == "resources" and len(rel) > 3 else "/".join(rel[:2])
        bucket = roots.setdefault(key, [0, 0])
        bucket[0] += 1
        bucket[1] += size
    asar = root / "resources" / "app.asar"
    inside = asar_entries(asar) if asar.is_file() else {}
    return {
        "files": files,
        "bytes": total,
        "asar_files": len(inside),
        "asar_unpacked_marked": sum(1 for e in inside.values() if e.get("unpacked")),
        "roots": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(roots.items())},
    }


def pruned(root: Path) -> list[str]:
    """Paths of every win-prune.json entry still in the tree."""
    left: list[str] = []
    for e in PRUNE["entries"]:
        full = root / e["path"]
        if "match" in e:
            left += [f"{e['path']}/{f.name}" for f in full.glob("*") if re.search(e["match"], f.name, re.I)]
        elif full.exists():
            left.append(e["path"])
    return left


def is_pruned(rel: str) -> bool:
    """True when win-prune.json removes this path (relative to win-unpacked)."""
    for e in PRUNE["entries"]:
        if "match" in e:
            parent, _, name = rel.rpartition("/")
            if parent == e["path"] and re.search(e["match"], name, re.I):
                return True
        elif rel == e["path"] or rel.startswith(e["path"] + "/"):
            return True
    return False


def assertions(root: Path) -> list[tuple[str, bool, str, str]]:
    """(kind, ok, name, detail) for every packaged-tree assertion."""
    out: list[tuple[str, bool, str, str]] = []

    def add(kind: str, name: str, problems: list[str]) -> None:
        out.append((kind, not problems, name, "; ".join(problems[:5]) + (" ..." if len(problems) > 5 else "")))

    inside = asar_entries(root / "resources" / "app.asar")
    darwin = sorted({p.split("/")[2] for p in inside if p.startswith("node_modules/@embedded-postgres/darwin-")})
    darwin += sorted(f"unpacked {d.name}" for d in (root / PG).glob("darwin-*"))
    add("removed", "no macOS PostgreSQL in app.asar or app.asar.unpacked", darwin)

    site = root / PRUNE["sitePackages"]
    add("removed", "no celerp-postgres package in site-packages",
        sorted(p.name for p in site.glob("celerp_postgres*")))

    add("removed", "every win-prune.json entry is absent", pruned(root))

    add("kept", "Windows PostgreSQL server and backup programs present",
        [p for p in PG_PROGRAMS if not (root / p).is_file()])

    unpacked = root / "resources" / "app.asar.unpacked"
    add("kept", "every app.asar entry marked unpacked exists on disk, apart from what win-prune.json removes",
        sorted(p for p, e in inside.items() if e.get("unpacked") and not (unpacked / p).is_file()
               and not is_pruned(f"resources/app.asar.unpacked/{p}")))

    app = root / "resources" / "app"
    add("kept", "celerp metadata and the app sources present",
        ([] if any(site.glob("celerp-*.dist-info/METADATA")) else ["celerp-*.dist-info"])
        + [f"resources/app/{d}" for d in ("celerp", "ui", "default_modules") if not (app / d).is_dir()])

    modules = app / "default_modules"
    add("kept", "no compiled Python under resources/app/default_modules",
        sorted(str(p.relative_to(root)) for p in modules.rglob("*")
               if p.suffix == ".pyc" or p.name == "__pycache__"))

    add("kept", "required licence texts present", [p for p in LICENCES if not (root / p).is_file()])

    same = [e for e in PRUNE["entries"] if "sameAs" in e]
    add("kept", "the kept copy of every removed duplicate is present",
        [e["sameAs"] for e in same if not (root / e["sameAs"]).is_file()])
    return out


def check(root: Path) -> int:
    results = assertions(root)
    for kind, ok, name, detail in results:
        print(f"{'PASS' if ok else 'FAIL'} [{kind}] {name}" + ("" if ok else f": {detail}"))
    failed = sum(1 for r in results if not r[1])
    print(f"{len(results) - failed}/{len(results)} passed")
    return 1 if failed else 0


UNINSTALLER = "Uninstall Celerp.exe"


def files(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def installed(build: Path, folder: Path) -> int:
    """The installer writes the build's files and its own uninstaller, so anything
    else was left by an earlier copy, anything short was never written, and a file
    whose bytes differ from the build's was written partly or not by this build."""
    want, have = files(build), files(folder) - {UNINSTALLER}
    missing, extra = sorted(want - have), sorted(have - want)
    differ = sorted(name for name in want & have if sha256(build / name) != sha256(folder / name))
    for kind, names in (("missing", missing), ("extra", extra), ("differs", differ)):
        for name in names[:40]:
            print(f"{kind}: {name}")
    if missing or extra or differ:
        print(f"FAIL {folder}: {len(missing)} missing, {len(extra)} extra, {len(differ)} differ against {build}")
        return 1
    print(f"PASS {folder}: the build's {len(want)} files, byte for byte (SHA-256), and the uninstaller")
    return 0


def main() -> int:
    # Paths under a profile carry the account name, which a Windows console's code page
    # may not hold; print those letters escaped rather than failing the check.
    sys.stdout.reconfigure(errors="backslashreplace")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("carve")
    c.add_argument("setup", type=Path)
    c.add_argument("out", type=Path)
    i = sub.add_parser("inventory")
    i.add_argument("root", type=Path)
    i.add_argument("--json", type=Path)
    k = sub.add_parser("check")
    k.add_argument("root", type=Path)
    n = sub.add_parser("installed")
    n.add_argument("build", type=Path)
    n.add_argument("folder", type=Path)
    a = p.parse_args()
    if a.cmd == "installed":
        return installed(a.build, a.folder)
    if a.cmd == "carve":
        return carve(a.setup, a.out)
    if a.cmd == "inventory":
        inv = inventory(a.root)
        text = json.dumps(inv, indent=1)
        if a.json:
            a.json.write_text(text + "\n", "utf-8")
        print(f"{a.root}: {inv['files']} files, {inv['bytes']} bytes, app.asar {inv['asar_files']} entries")
        return 0
    return check(a.root)


if __name__ == "__main__":
    raise SystemExit(main())
