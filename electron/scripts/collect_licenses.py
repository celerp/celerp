#!/usr/bin/env python3
"""Assemble (and verify) the third-party license bundle from real artifacts.

This copies license texts from the actually-bundled components — it does not
paraphrase or hard-code them:

  * PostgreSQL / OpenSSL : from the source trees the pg tools were built from
                           (--pg-licenses; macOS only).
  * Python interpreter   : the CPython license + bundled-lib licenses in each
                           python-build-standalone tree (--pbs-root, repeatable).
  * Python packages      : each <dist-info>/licenses (or, if the wheel shipped
                           none, the declared license metadata) under every
                           --site-packages (repeatable).
  * Electron / Chromium  : from node_modules/electron/dist.
  * npm packages         : license files found across --node-modules, including
                           transitive deps; falls back to the package.json
                           "license" field when no file is present.

Multiple --pbs-root / --site-packages are merged (the universal macOS app bundles
both the arm64 and x64 Python trees, so both are collected and checked).

Modes:
  collect (default) : write the bundle into --out.
  --check           : fail (exit 1) if any required component, npm package, or
                      installed Python distribution has neither a license file
                      nor declared license metadata.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

LICENSE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "COPYRIGHT*", "NOTICE*", "AUTHORS*")
# Components that MUST have a non-empty license entry in every packaged build.
REQUIRED_ALWAYS = ("python", "electron", "chromium")


def slug(name: str) -> str:
    """Folder-safe slug for a package name (handles npm scopes)."""
    return re.sub(r"[^A-Za-z0-9._-]", "__", name.lstrip("@"))


def is_license_name(n: str) -> bool:
    nl = n.lower()
    return (
        nl.startswith(("license", "licence", "copying", "copyright", "notice", "authors"))
        or nl.endswith(".terms")
        or "license" in nl
        or "licence" in nl
        or n == "PYTHON.json"
    )


def license_files(d: Path) -> list[Path]:
    out: list[Path] = []
    if d and d.is_dir():
        for pat in LICENSE_GLOBS:
            out += [p for p in d.glob(pat) if p.is_file()]
    return sorted(set(out), key=lambda p: p.name)


def _digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy_dedup(dest: Path, files: list[Path]) -> int:
    """Copy files into dest, skipping byte-identical duplicates and renaming
    name-collisions that differ in content. Merges with whatever is already
    there, so this is safe to call repeatedly (e.g. arm64 then x64). Returns the
    number of new files written.

    Deduping by content (not by name) is what lets us collect both arches without
    dropping a genuinely different file that happens to share a filename.
    """
    dest.mkdir(parents=True, exist_ok=True)
    by_hash: dict[str, str] = {}
    for ex in dest.iterdir():
        if ex.is_file():
            by_hash[_digest(ex)] = ex.name
    written = 0
    for f in files:
        try:
            d = _digest(f)
        except OSError:
            continue
        if d in by_hash:
            continue
        target = dest / f.name
        if target.exists() and _digest(target) != d:
            target = dest / f"{f.stem}.{d[:8]}{f.suffix}"
        try:
            shutil.copy2(f, target)
            by_hash[d] = target.name
            written += 1
        except OSError as e:
            print(f"  WARN: could not copy {f}: {e}", file=sys.stderr)
    return written


def non_empty_dir(d: Path) -> bool:
    return d.is_dir() and any(p.is_file() and p.stat().st_size > 0 for p in d.rglob("*"))


def pip_declared_license(meta_text: str) -> list[str]:
    """License lines declared in a wheel's METADATA (empty => none declared)."""
    lines: list[str] = []
    for ln in meta_text.splitlines():
        if ln.startswith("License-Expression:") and ln.split(":", 1)[1].strip():
            lines.append(ln)
        elif ln.startswith("Classifier: License"):
            lines.append(ln)
        elif ln.startswith("License:"):
            val = ln.split(":", 1)[1].strip()
            if val and val.upper() != "UNKNOWN":
                lines.append(ln)
    return lines


def npm_declared_license(data: dict) -> str | None:
    lic = data.get("license")
    if isinstance(lic, str) and lic.strip():
        return lic.strip()
    if isinstance(lic, dict) and lic.get("type"):
        return str(lic["type"])
    legacy = data.get("licenses")  # deprecated array form
    if isinstance(legacy, list):
        types = [x.get("type") for x in legacy if isinstance(x, dict) and x.get("type")]
        if types:
            return ", ".join(types)
    return None


def pkg_name_from_dist_info(di: Path) -> str:
    meta = di / "METADATA"
    if meta.is_file():
        m = re.search(r"^Name:\s*(.+)$", meta.read_text(errors="replace"), re.M)
        if m:
            return m.group(1).strip()
    return di.name.rsplit("-", 2)[0]


def pkg_version_from_dist_info(di: Path) -> str:
    meta = di / "METADATA"
    if meta.is_file():
        m = re.search(r"^Version:\s*(.+)$", meta.read_text(errors="replace"), re.M)
        if m:
            return m.group(1).strip()
    stem = di.name[: -len(".dist-info")] if di.name.endswith(".dist-info") else di.name
    return stem.rsplit("-", 1)[1] if "-" in stem else ""


def pkg_dir(name: str, version: str) -> str:
    """Folder key including version, so two versions of the same package can
    never merge or overwrite each other (mac arm64/x64 are installed separately
    and could in principle resolve different versions)."""
    return f"{slug(name)}__{slug(version) if version else 'unknown'}"


def iter_npm_packages(node_modules: Path):
    """Yield (name, version, pkg_dir, package_json_data) for every package found
    under node_modules, including transitive deps (pnpm/.pnpm and nesting)."""
    if not node_modules or not node_modules.is_dir():
        return
    for pj in node_modules.rglob("package.json"):
        try:
            data = json.loads(pj.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        name = data.get("name")
        if isinstance(name, str) and name:
            yield name, data.get("version", ""), pj.parent, data


# ── component collectors ─────────────────────────────────────────────────────

def collect_pg(pg_licenses: Path | None, out: Path) -> None:
    if not pg_licenses or not pg_licenses.is_dir():
        return  # non-macOS, or pg tools not built (phase 1 is macOS-only)
    for comp in ("postgresql", "openssl", "zlib"):
        src = pg_licenses / comp
        if src.is_dir():
            copy_dedup(out / comp, license_files(src))


def collect_python(pbs_roots: list[str], out: Path, extra: str | None = None) -> None:
    for root in pbs_roots:
        base = Path(root)
        if not base.is_dir():
            continue
        found = [
            p for p in base.rglob("*")
            if p.is_file()
            and is_license_name(p.name)
            and "site-packages" not in p.parts
            and ".dist-info" not in str(p)
        ]
        copy_dedup(out / "python", found)
    # The install_only python-build-standalone archive strips the licenses for the
    # libraries it compiles in (OpenSSL, SQLite, libffi, ncurses, xz, ...), which we
    # still redistribute inside the extension modules. Ship the curated set.
    if extra and Path(extra).is_dir():
        copy_dedup(out / "python", [p for p in Path(extra).iterdir() if p.is_file()])


def collect_pip_packages(site_packages: list[str], out: Path) -> None:
    for spd in site_packages:
        sp = Path(spd)
        if not sp.is_dir():
            continue
        for di in sorted(sp.glob("*.dist-info")):
            name = pkg_name_from_dist_info(di)
            dest = out / "python-packages" / pkg_dir(name, pkg_version_from_dist_info(di))
            files = license_files(di / "licenses") + license_files(di)
            if files:
                copy_dedup(dest, files)
                continue
            meta = di / "METADATA"
            decl = pip_declared_license(meta.read_text(errors="replace")) if meta.is_file() else []
            if decl:
                dest.mkdir(parents=True, exist_ok=True)
                (dest / "METADATA-LICENSE.txt").write_text(
                    f"{name}\n\nNo license file was included in this wheel.\n"
                    "Declared license metadata:\n\n" + "\n".join(decl) + "\n"
                )
            # else: no file and nothing declared -> no entry -> --check fails.


def collect_npm(node_modules: Path | None, out: Path) -> None:
    if not node_modules:
        return
    for name, ver, pkgdir, data in iter_npm_packages(node_modules):
        dest = out / "npm-packages" / pkg_dir(name, ver)
        files = license_files(pkgdir)
        if files:
            copy_dedup(dest, files)
            continue
        decl = npm_declared_license(data)
        if decl:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "LICENSE-FROM-PACKAGE-JSON.txt").write_text(
                f"{name}@{ver}\n\nNo license file was included with this package.\n"
                f"Declared license (SPDX, from package.json): {decl}\n"
            )
        # else: no file and nothing declared -> no entry -> --check fails.


def collect_electron(node_modules: Path | None, out: Path) -> None:
    if not node_modules:
        return
    dist = node_modules / "electron" / "dist"
    copy_dedup(out / "electron", license_files(dist) + license_files(node_modules / "electron"))
    if dist.is_dir():
        chromium = [p for p in dist.glob("LICENSES.chromium.html") if p.is_file()]
        if chromium:
            copy_dedup(out / "chromium", chromium)


def embedded_postgres_roots(node_modules: Path) -> list[Path]:
    """Packages that actually ship the PostgreSQL *server* binaries + native libs.

    Only the @embedded-postgres/<platform> packages carry a `native/` payload. The
    bare `embedded-postgres` wrapper is JS-only (handled by the generic npm pass)
    and must NOT trigger native-lib attribution — otherwise Linux/Windows, which
    install the wrapper but no native package, would be required to ship licenses
    for server binaries they don't bundle.
    """
    scope = node_modules / "@embedded-postgres"
    if not scope.is_dir():
        return []
    return [
        d.resolve() for d in scope.iterdir()
        if d.is_dir() and (d.resolve() / "native").is_dir()
    ]


def collect_embedded_postgres(node_modules: Path | None, out: Path, extra: str | None = None) -> None:
    """@embedded-postgres ships the actual PostgreSQL *server* binaries plus the
    native libs they link, but only its own wrapper LICENSE.md. Copy that, then
    add the curated upstream license texts for the bundled native libs (ICU,
    Kerberos, libiconv/gettext LGPL, lz4, zstd, libxml2, libedit, libuuid, zlib)
    which are NOT present in the package. Only runs when the server is actually
    bundled (darwin), so non-macOS builds get no embedded-postgres/ folder."""
    if not node_modules:
        return
    roots = embedded_postgres_roots(node_modules)
    if not roots:
        return
    files: list[Path] = []
    for r in roots:
        if r.is_dir():
            files += [p for p in r.rglob("*") if p.is_file() and is_license_name(p.name)]
    copy_dedup(out / "embedded-postgres", files)
    if extra and Path(extra).is_dir():
        copy_dedup(out / "embedded-postgres", [p for p in Path(extra).iterdir() if p.is_file()])


# ── modes ────────────────────────────────────────────────────────────────────

def _count(out: Path, sub: str) -> int:
    d = out / sub
    return sum(1 for c in d.iterdir() if non_empty_dir(c)) if d.is_dir() else 0


def do_collect(a: argparse.Namespace) -> int:
    out = Path(a.out)
    if out.is_dir():
        for child in out.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    collect_pg(Path(a.pg_licenses) if a.pg_licenses else None, out)
    collect_python(a.pbs_root or [], out, a.extra_python)
    collect_pip_packages(a.site_packages or [], out)
    collect_electron(Path(a.node_modules) if a.node_modules else None, out)
    collect_npm(Path(a.node_modules) if a.node_modules else None, out)
    collect_embedded_postgres(Path(a.node_modules) if a.node_modules else None, out, a.extra_embedded_postgres)

    summary = [
        f"python-packages : {_count(out, 'python-packages')}",
        f"npm-packages    : {_count(out, 'npm-packages')}",
    ]
    for comp in ("postgresql", "openssl", "embedded-postgres", "python", "electron", "chromium"):
        if (out / comp).is_dir():
            summary.append(f"{comp:15} : present")
    (out / "THIRD-PARTY-NOTICES.txt").write_text(
        "THIRD-PARTY NOTICES\n===================\n\n"
        "Full license texts for the third-party components bundled in this build\n"
        "are in the sibling folders. Generated by electron/scripts/collect_licenses.py\n"
        "from the actual bundled artifacts.\n\n"
        "zlib is linked dynamically against the OS-provided libz and is not\n"
        "redistributed unless a zlib/ folder appears below.\n\n" + "\n".join(summary) + "\n"
    )
    print("Collected license bundle:\n  " + "\n  ".join(summary))
    return 0


def do_check(a: argparse.Namespace) -> int:
    out = Path(a.out)
    missing: list[str] = []

    required = list(REQUIRED_ALWAYS)
    if a.pg_licenses:
        required += ["postgresql", "openssl"]
    for comp in required:
        if not non_empty_dir(out / comp):
            missing.append(f"required component '{comp}' has no license text")

    # The interpreter's bundled-lib license set must land too (openssl-3 sentinel).
    if a.extra_python and not (out / "python" / "LICENSE.openssl-3.txt").is_file():
        missing.append("python bundled-lib license texts missing (curated set absent)")

    seen: set[str] = set()
    if a.node_modules:
        nm = Path(a.node_modules)
        for name, ver, _dir, _data in iter_npm_packages(nm):
            key = pkg_dir(name, ver)
            if key in seen:
                continue
            seen.add(key)
            if not non_empty_dir(out / "npm-packages" / key):
                missing.append(f"npm package '{name}@{ver}' has no license file or declared license")
        # @embedded-postgres ships PostgreSQL server binaries + native libs.
        if embedded_postgres_roots(nm):
            if not non_empty_dir(out / "embedded-postgres"):
                missing.append("embedded-postgres (bundled PostgreSQL server binaries) has no license text")
            # The curated upstream texts for the bundled native libs must land too.
            # zlib-LICENSE.txt is the sentinel (present in every curated set; the
            # bare @embedded-postgres package ships only LICENSE.md). Its absence
            # means the --extra-embedded-postgres wiring is broken for this platform.
            elif not (out / "embedded-postgres" / "zlib-LICENSE.txt").is_file():
                missing.append("embedded-postgres native-lib license texts missing (curated set absent)")

    seen_py: set[str] = set()
    for spd in (a.site_packages or []):
        sp = Path(spd)
        for di in sp.glob("*.dist-info"):
            name = pkg_name_from_dist_info(di)
            ver = pkg_version_from_dist_info(di)
            key = pkg_dir(name, ver)
            if key in seen_py:
                continue
            seen_py.add(key)
            if not non_empty_dir(out / "python-packages" / key):
                missing.append(f"python package '{name} {ver}' has no license file or declared license")

    if missing:
        print("LICENSE CHECK FAILED — missing attribution for:", file=sys.stderr)
        for m in missing:
            print(f"  - {m}", file=sys.stderr)
        return 1
    print("License check passed: all bundled components have attribution.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", required=True, help="output licenses/ dir")
    p.add_argument("--node-modules")
    p.add_argument("--site-packages", action="append", help="repeatable")
    p.add_argument("--pbs-root", action="append", help="python-build-standalone tree; repeatable")
    p.add_argument("--extra-python", help="curated license texts for the interpreter's bundled native libs")
    p.add_argument("--pg-licenses", help="dir with postgresql/ openssl/ license subdirs (macOS)")
    p.add_argument("--extra-embedded-postgres", help="curated upstream license texts for the embedded server's bundled native libs")
    p.add_argument("--package-json", help="(unused; kept for compatibility)")
    p.add_argument("--check", action="store_true", help="verify instead of collect")
    a = p.parse_args()
    return do_check(a) if a.check else do_collect(a)


if __name__ == "__main__":
    raise SystemExit(main())