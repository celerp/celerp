# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Write the countries with installs, as the relay counts them, for the pack check.

    python scripts/install_countries.py --url <relay endpoint> [--out <file>]

The relay answers a JSON object of two-letter upper-case country codes to install counts. The
counts are written to celerp/localization/install_countries.json (or --out) through a temporary
file and a rename, so a relay error, an answer that is not counts or a failing write exits
non-zero and leaves the file as it was. Run at build time only; nothing reads it at run time
except check_install_countries_have_packs.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "celerp" / "localization" / "install_countries.json"


def _counts(body: bytes) -> dict[str, int]:
    data = json.loads(body)
    if not isinstance(data, dict) or not all(
            isinstance(code, str) and len(code) == 2 and code.isascii() and code.isalpha() and code.isupper()
            and type(count) is int and count >= 0 for code, count in data.items()):
        raise ValueError("the relay answer is not install counts by country code")
    return dict(sorted(data.items()))


def _write(out: Path, counts: dict[str, int]) -> None:
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(counts, f, indent=2)
            f.write("\n")
        os.replace(tmp, out)
    except BaseException:
        os.unlink(tmp)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    try:
        with urllib.request.urlopen(args.url, timeout=30) as response:
            counts = _counts(response.read())
        _write(args.out, counts)
    except (OSError, ValueError) as error:
        print(f"install countries not written: {error}", file=sys.stderr)
        return 1
    print(f"{len(counts)} countries written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
