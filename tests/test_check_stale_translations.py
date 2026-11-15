# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""scripts/check_stale_translations.py finds an English change that no translation followed,
and passes it once it is translated or queued with the English in every locale."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from check_stale_translations import stale  # noqa: E402


def _write(root: Path, d: str, catalogs: dict[str, dict[str, str]]) -> None:
    (root / d).mkdir(parents=True, exist_ok=True)
    for code, cat in catalogs.items():
        (root / d / f"{code}.json").write_text(json.dumps(cat), encoding="utf-8")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def test_an_english_change_no_translation_followed_is_stale_until_translated_or_queued(tmp_path):
    mod = "default_modules/celerp-x/celerp_x/locales"
    for d in ("ui/locales", mod):
        _write(tmp_path, d, {"en": {"a": "List", "b": "Same"}, "de": {"a": "Liste", "b": "Gleich"},
                             "th": {"a": "รายการ", "b": "เหมือน"}})
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")

    for d in ("ui/locales", mod):
        _write(tmp_path, d, {"en": {"a": "Quotation", "b": "Same"}})
    assert stale("HEAD", tmp_path) == {"ui/locales": ["a"], mod: ["a"]}

    # One locale translated: the change reached the translators.
    _write(tmp_path, "ui/locales", {"de": {"a": "Angebot", "b": "Gleich"}})
    # Queued: the English in every locale until it is translated.
    _write(tmp_path, mod, {"de": {"a": "Quotation", "b": "Gleich"}, "th": {"a": "Quotation", "b": "เหมือน"}})
    assert stale("HEAD", tmp_path) == {}
