#!/usr/bin/env python3
# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""License consistency tool for the Celerp multi-license repo.

Encodes the path -> license tier policy (mirrors LICENSING.md) and enforces it across Python source
headers, per-module LICENSE files, and module manifest `license` fields.

  python scripts/licenses.py check   # CI: exit 1 on any mismatch
  python scripts/licenses.py fix     # rewrite SPDX id + copyright line + manifest license to match

`fix` only rewrites an existing `# SPDX-License-Identifier:` line (and the `# Copyright ...` line directly
above it) and the manifest `"license"` value. It never inserts headers or reflows code; files missing a
header are reported by `check`, not silently modified.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROP = "LicenseRef-Proprietary"
MIT = "MIT"
BUSL = "BUSL-1.1"

MIT_MODULES = {
    "accounting", "contacts", "dashboard", "docs", "inventory",
    "labels", "manufacturing", "reports", "subscriptions", "verticals",
}
PROP_MODULES = {"ai", "backup", "connectors"}
BUSL_MODULES = {"admin"}

# Proprietary subtrees inside the otherwise-BUSL kernel.
_PROP_KERNEL = ("celerp/gateway/", "celerp/ai/", "celerp/cloud/", "celerp/output/")


def tier_for(rel: str) -> str | None:
    """Return the license tier for a repo-relative path, or None if outside the policy."""
    p = rel.replace("\\", "/")
    if p == "celerp/session_gate.py" or p.startswith(_PROP_KERNEL) or p.startswith("ui/"):
        return PROP
    if p.startswith("default_modules/celerp-"):
        mod = p.split("/", 2)[1][len("celerp-"):]
        if mod in PROP_MODULES:
            return PROP
        if mod in MIT_MODULES:
            return MIT
        if mod in BUSL_MODULES:
            return BUSL
        return BUSL
    if p.startswith("celerp/"):
        return BUSL
    return None


_SPDX_RE = re.compile(r"^# SPDX-License-Identifier:\s*(.+?)\s*$")


def _iter_py():
    for base in ("celerp", "default_modules", "ui"):
        for f in (ROOT / base).rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            yield f


def _check_or_fix_file(f: Path, do_fix: bool) -> list[str]:
    rel = str(f.relative_to(ROOT))
    tier = tier_for(rel)
    if tier is None:
        return []
    want_spdx = tier
    lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
    problems: list[str] = []
    spdx_idx = next((i for i, ln in enumerate(lines) if _SPDX_RE.match(ln.rstrip("\n"))), None)
    if spdx_idx is None:
        if f.stat().st_size == 0:
            return []  # empty __init__.py etc. - no header needed
        return [f"{rel}: missing SPDX header (want {want_spdx})"]
    cur = _SPDX_RE.match(lines[spdx_idx].rstrip("\n")).group(1)
    nl = "\n"
    changed = False
    if cur != want_spdx:
        problems.append(f"{rel}: SPDX {cur} -> {want_spdx}")
        if do_fix:
            lines[spdx_idx] = f"# SPDX-License-Identifier: {want_spdx}{nl}"
        changed = True
    # An MIT file must not carry an "All rights reserved" copyright flourish (it contradicts the grant).
    if want_spdx == MIT and spdx_idx > 0 and "All rights reserved" in lines[spdx_idx - 1]:
        problems.append(f"{rel}: strip 'All rights reserved' (MIT)")
        if do_fix:
            lines[spdx_idx - 1] = lines[spdx_idx - 1].replace(". All rights reserved.", "").replace(" All rights reserved.", "")
        changed = True
    if do_fix and changed:
        f.write_text("".join(lines), encoding="utf-8")
    return problems


_MANIFEST_LICENSE = {
    PROP: "LicenseRef-Proprietary", MIT: "MIT", BUSL: "BUSL-1.1",
}
_MAN_RE = re.compile(r'("license":\s*")([^"]*)(")')


def _check_or_fix_manifests(do_fix: bool) -> list[str]:
    problems: list[str] = []
    for f in (ROOT / "default_modules").glob("celerp-*/**/__init__.py"):
        if "__pycache__" in f.parts:
            continue
        rel = str(f.relative_to(ROOT))
        text = f.read_text(encoding="utf-8")
        if '"license"' not in text:
            continue
        mod = rel.split("/", 2)[1][len("celerp-"):]
        tier = PROP if mod in PROP_MODULES else MIT if mod in MIT_MODULES else BUSL
        want = _MANIFEST_LICENSE[tier]
        m = _MAN_RE.search(text)
        if m and m.group(2) != want:
            problems.append(f"{rel}: manifest license {m.group(2)} -> {want}")
            if do_fix:
                f.write_text(_MAN_RE.sub(rf'\g<1>{want}\g<3>', text, count=1), encoding="utf-8")
    return problems


def _check_or_fix_presets(do_fix: bool) -> list[str]:
    """Vertical preset JSONs carry a vestigial `license` field; keep it consistent with the module (MIT)."""
    problems: list[str] = []
    preset_dir = ROOT / "default_modules" / "celerp-verticals" / "celerp_verticals" / "presets"
    for f in preset_dir.glob("*.json"):
        text = f.read_text(encoding="utf-8")
        m = _MAN_RE.search(text)
        if m and m.group(2) != "MIT":
            problems.append(f"{f.relative_to(ROOT)}: preset license {m.group(2)} -> MIT")
            if do_fix:
                f.write_text(_MAN_RE.sub(r'\g<1>MIT\g<3>', text, count=1), encoding="utf-8")
    return problems


def _check_module_license_files() -> list[str]:
    problems: list[str] = []
    for d in sorted((ROOT / "default_modules").glob("celerp-*")):
        if not d.is_dir():
            continue
        mod = d.name[len("celerp-"):]
        if mod in MIT_MODULES and not (d / "LICENSE").exists():
            problems.append(f"{d.relative_to(ROOT)}: MIT module missing LICENSE file")
    return problems


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    do_fix = mode == "fix"
    if mode not in ("check", "fix"):
        print("usage: licenses.py [check|fix]")
        return 2
    problems: list[str] = []
    for f in _iter_py():
        problems += _check_or_fix_file(f, do_fix)
    problems += _check_or_fix_manifests(do_fix)
    problems += _check_or_fix_presets(do_fix)
    problems += _check_module_license_files()
    if not problems:
        print("license check: OK")
        return 0
    verb = "fixed" if do_fix else "MISMATCH"
    for p in problems:
        print(f"  [{verb}] {p}")
    print(f"\n{len(problems)} item(s).")
    return 0 if do_fix else 1


if __name__ == "__main__":
    raise SystemExit(main())
