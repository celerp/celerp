# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A file that carries one company is called a company copy, in every language.

Whole-installation backups keep their own name (the Backup tab and System Recovery),
so a company copy string may point at that tab, but must never call the company copy
itself a backup, a handoff, an archive, a bundle or a package.
"""

import json
import re
from pathlib import Path

import pytest

LOCALES = Path(__file__).resolve().parent.parent / "ui" / "locales"

# The company copy strings outside the company_backup namespace.
EXTRA_KEYS = (
    "setup.card_restore",
    "setup.card_restore_desc",
    "setup.card_restore_from_backup",
    "setup.option_restore_title",
    "setup.option_restore_desc",
    "setup.start_company_explain",
    "setup.later_from_dashboard",
    "settings.download_backup_first",
)

FORBIDDEN = {
    "en": r"(?i)\b(backups?|handoffs?|archives?|bundles?|packages?)\b",
    "de": r"(?i)sicherung|backup|übergabe",
    "fr": r"(?i)sauvegarde|archive",
    "es": r"(?i)copia de seguridad|respaldo",
    "it": r"(?i)backup",
    "pt": r"(?i)backup",
    "id": r"(?i)cadangan",
    "ja": r"バックアップ",
    "th": r"สำรอง",
    "vi": r"(?i)sao lưu",
    "ar": r"احتياطي",
    "am": r"ምትኬ",
}


def _catalog(lang: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


def _copy_strings(catalog: dict[str, str]) -> dict[str, str]:
    keys = [k for k in catalog if k.startswith("company_backup.")] + list(EXTRA_KEYS)
    return {k: catalog[k] for k in keys}


@pytest.mark.parametrize("lang", sorted(FORBIDDEN))
def test_company_copy_strings_never_call_it_a_backup(lang):
    catalog = _catalog(lang)
    # Pointing at the Backup tab, where System Recovery lives, is allowed.
    tab = re.escape(catalog["settings.tab_backup"])
    recovery = re.escape(catalog["system_recovery.title"])
    allowed = re.compile(rf"[,،、]\s*{tab}|{recovery}")
    wrong = {
        key: text
        for key, text in _copy_strings(catalog).items()
        if re.search(FORBIDDEN[lang], allowed.sub("", text))
    }
    assert wrong == {}


def test_english_names_the_company_copy():
    en = _catalog("en")
    assert en["company_backup.download"] == "Download company copy"
    assert en["company_backup.restore_title"] == "Open a company copy"
    assert en["setup.option_restore_title"] == "Open a company copy"
    assert en["company_backup.handoff_link_label"] == "Or paste a company copy link"
