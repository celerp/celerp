# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""No new emoji used as icons: the pages draw their icons with ``ui.components.icons.icon``.

The emoji already in the UI code are listed below, per file, and in the English strings, per key. The
lists only get shorter: a file or string may not gain an emoji, and the entries come out as the emoji
are replaced by icons. The check marks and crosses (✓ and ✗) that the pages and the terminal print
as status text are not icons and are always allowed, and so is ``celerp/cli.py``, which is not UI code.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Pictographs and the symbol characters pages use as icons. The variation selector that turns a
# symbol into its emoji form is not counted: it only ever follows one of these.
EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u26FF\u2700-\u27BF\u2139\u2B50\u2B1B\u2B1C\u231A\u231B\u23E9-\u23FF\u2705\u274C\u2728]")
STATUS_TEXT = frozenset("\u2713\u2717")  # check mark, ballot x

_SKIPPED_DIRS = {"tests", "__pycache__", "fixtures", "vendor", "locales", "node_modules"}

# Emoji (not counting the status text above) per UI source file, as of the icon helper's arrival.
ALLOWED_IN_CODE: dict[str, int] = {
    "default_modules/celerp-ai/celerp_ai/ui_routes.py": 10,
    "default_modules/celerp-fixed-assets/__init__.py": 2,
    "default_modules/celerp-labels/__init__.py": 2,
    "default_modules/celerp-manufacturing/__init__.py": 3,
    "ui/components/files.py": 3,
    "ui/components/shell.py": 2,
    "ui/routes/accounting.py": 1,
    "ui/routes/contacts.py": 5,
    "ui/routes/csv_import.py": 1,
    "ui/routes/dashboard.py": 3,
    "ui/routes/documents.py": 21,
    "ui/routes/inventory.py": 2,
    "ui/routes/manufacturing.py": 3,
    "ui/routes/modules_page.py": 1,
    "ui/routes/reports.py": 1,
    "ui/routes/search.py": 7,
    "ui/routes/settings.py": 7,
    "ui/routes/settings_cloud.py": 6,
    "ui/routes/settings_connectors.py": 7,
    "ui/routes/settings_inventory.py": 2,
    "ui/routes/setup.py": 4,
}

# English strings that carry an emoji today.
ALLOWED_IN_STRINGS: frozenset[str] = frozenset({
    "acct.cf_methods_disagree",
    "acct.imbalance_detected",
    "acct.out_of_balance",
    "ai.attached_files",
    "ai.scenario_batch_bills_user",
    "ai.scenario_bulk_catalog_reply",
    "ai.scenario_bulk_catalog_user",
    "ai.scenario_discrepancy_audit_reply",
    "ai.scenario_reconcile_user",
    "ai.scenario_smart_restock_reply",
    "btn._make_primary",
    "btn._memory",
    "btn._print_labels",
    "doc._labels",
    "inv.hero_product_image",
    "inv.u0001f4c1",
    "inv.u0001f4cb_duplicate",
    "inv.u1f4e6_archive",
    "inv.u23f3_expire",
    "inv.u2702_split",
    "label._primary",
    "msg._web_access",
    "msg.u0001f512",
    "nav.company_details",
    "nav.doctor",
    "nav.modules",
    "nav.settings",
    "page._access_celerp_from_anywhere",
    "settings._changing_the_database_requires_a_restart_and_data",
    "settings._cost_price_is_tracked_peritem_from_purchase_docum",
    "settings._default",
})


def _emoji(text: str) -> list[str]:
    return [ch for ch in EMOJI.findall(text) if ch not in STATUS_TEXT]


def _ui_sources() -> list[Path]:
    """Every page-building Python and JavaScript file: the UI layer and the modules (not their tests)."""
    found = []
    for top in ("ui", "default_modules"):
        for path in (ROOT / top).rglob("*"):
            rel = path.relative_to(ROOT)
            if path.suffix in (".py", ".js") and not path.name.endswith(".min.js") and not (_SKIPPED_DIRS & set(rel.parts)):
                found.append(path)
    return sorted(found)


def _in_code() -> dict[str, int]:
    counts = {}
    for path in _ui_sources():
        n = len(_emoji(path.read_text(encoding="utf-8")))
        if n:
            counts[path.relative_to(ROOT).as_posix()] = n
    return counts


def test_ui_code_gains_no_emoji():
    grown = {f: (n, ALLOWED_IN_CODE.get(f, 0)) for f, n in _in_code().items() if n > ALLOWED_IN_CODE.get(f, 0)}
    assert not grown, (
        "Use ui.components.icons.icon(...) instead of an emoji as an icon (file: found, allowed): "
        f"{grown}. Status text may use the check mark or the cross.")


def test_the_code_allow_list_has_no_stale_entries():
    present = _in_code()
    stale = sorted(f for f in ALLOWED_IN_CODE if f not in present)
    assert not stale, f"These files no longer have an emoji; remove them from ALLOWED_IN_CODE: {stale}"


def test_english_strings_gain_no_emoji():
    strings = json.loads((ROOT / "ui" / "locales" / "en.json").read_text(encoding="utf-8"))
    new = sorted(k for k, v in strings.items() if isinstance(v, str) and _emoji(v) and k not in ALLOWED_IN_STRINGS)
    assert not new, f"These strings carry an emoji; the page draws an icon beside the text instead: {new}"
    stale = sorted(k for k in ALLOWED_IN_STRINGS if not _emoji(str(strings.get(k, ""))))
    assert not stale, f"These strings no longer have an emoji; remove them from ALLOWED_IN_STRINGS: {stale}"


def test_the_check_finds_an_emoji():
    """The scan itself: an icon emoji is found, the status marks and plain text are not."""
    assert _emoji('Span("\U0001F3E2", cls="section-icon")') == ["\U0001F3E2"]
    assert _emoji("\u2699\ufe0f Settings") == ["\u2699"]
    assert _emoji("\u2713 Saved \u2717 Failed") == []
    assert _emoji("plain text, \u00e9\u2014\u2192") == []
