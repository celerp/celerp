# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fail when an English string changed but no locale's translation followed it.

    python scripts/check_stale_translations.py --base <ref>

A string whose English text differs from <ref> while every other locale still holds the text
it held at <ref> (and that text is not the new English) is stale: the change never reached the
translators. Translate it, or queue it in tests/i18n_pending_translation.json with the English
in every locale until it is translated. Covers ui/locales and every bundled module's locales.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _at(ref: str, path: Path, root: Path) -> dict[str, str]:
    r = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{path.as_posix()}"],
                       capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else {}


def stale(base: str, root: Path = ROOT) -> dict[str, list[str]]:
    """Each locale directory with its stale keys, from <base> to the working tree."""
    out: dict[str, list[str]] = {}
    for d in [Path("ui/locales"), *sorted(p.relative_to(root) for p in root.glob("default_modules/*/*/locales"))]:
        if not (root / d / "en.json").exists():
            continue
        en, en_base = json.loads((root / d / "en.json").read_text(encoding="utf-8")), _at(base, d / "en.json", root)
        changed = [k for k in en if k in en_base and en_base[k] != en[k]]
        codes = sorted(p.stem for p in (root / d).glob("*.json") if p.stem != "en")
        if not changed or not codes:
            continue
        catalogs = {c: (json.loads((root / d / f"{c}.json").read_text(encoding="utf-8")), _at(base, d / f"{c}.json", root))
                    for c in codes}
        keys = [k for k in changed
                if all(k in now and now[k] == then.get(k) and now[k] != en[k] for now, then in catalogs.values())]
        if keys:
            out[d.as_posix()] = keys
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    found = stale(ap.parse_args().base)
    for d, keys in found.items():
        print(f"{d}: {len(keys)} English string(s) changed with every translation left as before:")
        for k in keys:
            print(f"  {k}")
    if found:
        print("Translate them, or queue them in tests/i18n_pending_translation.json (English in every locale).")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
