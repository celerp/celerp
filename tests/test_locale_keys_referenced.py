# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every string in the company copy, accountant access, firm view, account sync, migration
and accountant notice sections of the English catalog is used by the code, so none is
translated twelve times and never shown."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ("company_backup.", "accountant_access.", "firm_view.", "sync_accounts.", "migration.",
            "notice.peer_", "notice.online_fee", "notice.xero_")


def _shipped_source() -> str:
    parts = []
    for top in ("celerp", "ui", "default_modules", "electron"):
        for path in (ROOT / top).rglob("*"):
            if (path.suffix in (".py", ".js", ".html") and path.is_file()
                    and not {"tests", "locales", "node_modules"} & set(path.parts)):
                parts.append(path.read_text(errors="ignore"))
    return "\n".join(parts)


def _referenced(key: str, literals: set[str], templates: list[tuple[str, str]],
                prefixes: list[str]) -> bool:
    """Used as written, built by an f-string or a concatenation, or a notice's title or
    body read from the notice name."""
    return (key in literals
            or any(key.startswith(head) and key.endswith(tail) and len(key) > len(head) + len(tail)
                   for head, tail in templates)
            or any(key.startswith(head) and len(key) > len(head) for head in prefixes)
            or key.rsplit(".", 1)[0] in literals)


def test_no_orphan_locale_keys():
    source = _shipped_source()
    literals = set(re.findall(r"[\"']([a-z_]+\.[a-z0-9_.]+)[\"']", source))
    templates = re.findall(r"f[\"']([a-z_]+\.[a-z0-9_.]*)\{[^}]*\}([a-z0-9_.]*)[\"']", source)
    prefixes = re.findall(r"[\"']([a-z_]+\.[a-z0-9_.]*)[\"']\s*\+", source)
    catalog = json.loads((ROOT / "ui" / "locales" / "en.json").read_text())
    unused = [key for key in catalog if key.startswith(SECTIONS)
              and not _referenced(key, literals, templates, prefixes)]
    assert unused == []
