# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Export the help bundle: every shipped core and first-party guide with its fresh
translations, images and the renamed or removed guides from the ledger.

The release workflow attaches the output as help.json to each release, beside
openapi.json and importers.json, and the help center publishes it. Nothing is written
unless the ledger and translation checks and every bundle rule pass. Guide dates come
from git, so this needs a full clone.

    python scripts/export_help.py               # write help.json in place
    python scripts/export_help.py --out path     # write elsewhere
"""
from __future__ import annotations

import argparse
from pathlib import Path

from celerp.help.assemble import build_bundle, git_date
from celerp.help.bundle import bundle_problems
from celerp.help.checks import check_ledger, check_translations, shipped_pages
from celerp.help.ledger import write_data


def export(path, dates=git_date) -> dict:
    """Write the bundle to ``path`` atomically and return it. Raises SystemExit listing
    every problem, with nothing written; an OSError from the write leaves no file."""
    pages = shipped_pages()
    problems = check_ledger(pages) + check_translations(pages)
    try:
        bundle = build_bundle(dates)
    except (RuntimeError, ValueError) as e:
        problems.append(str(e))
    else:
        problems += [f"help.json: {p}" for p in bundle_problems(bundle)]
    if problems:
        raise SystemExit("help.json not written:\n" + "\n".join(problems))
    write_data(Path(path), bundle)
    return bundle


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="help.json", help="output path")
    args = parser.parse_args()
    data = export(args.out)
    print(f"Wrote {args.out}: {len(data['topics'])} guides")
