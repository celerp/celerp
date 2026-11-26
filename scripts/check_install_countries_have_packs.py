# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Fail when a country with installs has no locked country pack.

    python scripts/check_install_countries_have_packs.py [install_countries.json]

Every code in celerp/localization/install_countries.json (written by install_countries.py) must
have an entry in celerp/localization/packs.lock.json. PT is excepted: its stored tax list is kept
as it is.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOCALIZATION = Path(__file__).resolve().parent.parent / "celerp" / "localization"
EXCEPTED = frozenset({"PT"})


def missing(path: Path) -> list[str]:
    """The codes listed in *path* with no locked pack, PT excepted, sorted."""
    listed = json.loads(Path(path).read_text(encoding="utf-8"))
    locked = json.loads((LOCALIZATION / "packs.lock.json").read_text(encoding="utf-8"))
    return sorted(code for code in listed if code not in locked and code not in EXCEPTED)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else LOCALIZATION / "install_countries.json"
    if not path.is_file():
        print(f"{path} is missing; write it with scripts/install_countries.py")
        return 1
    codes = missing(path)
    for code in codes:
        print(f"{code} has installs but no locked country pack in packs.lock.json")
    return 1 if codes else 0


if __name__ == "__main__":
    sys.exit(main())
