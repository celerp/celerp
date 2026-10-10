# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Every call to accounting_roles.refusal() in production code names a key that
ui/locales/en.json actually defines. A typo'd or renamed-but-not-migrated key
renders the raw key string to the user instead of a message; nothing else in the
suite catches that, because test_i18n.py only checks the catalog's internal
consistency (placeholder parity, keyset completeness across the 12 shipped
locales), never whether the call sites actually use what the catalog has.

Once a key is confirmed present here, test_i18n.py's
test_release_complete_locales_have_full_keyset and
test_every_shipped_locale_placeholder_parity already guarantee it exists with
matching {placeholder} sets in all 12 shipped locales (en is in
_COMPLETE_LOCALES there), so this file does not re-check locales itself."""

from __future__ import annotations

import ast
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ROOTS = [_ROOT / "celerp", _ROOT / "default_modules"]


def _en_keys() -> set[str]:
    return set(json.loads((_ROOT / "ui" / "locales" / "en.json").read_text(encoding="utf-8")))


def _literal_or_prefix(node: ast.expr):
    """A refusal() first argument: an exact string literal, or - for an f-string
    whose leading parts are literal text - the literal prefix before the first
    interpolated value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, "exact"
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                break
        if parts:
            return "".join(parts), "prefix"
    return None, None


def _refusal_calls():
    """Every refusal(key, ...) call site in production code (celerp/ and
    default_modules/, excluding tests), as (file:line, key_or_prefix, kind)."""
    calls = []
    for root in _ROOTS:
        for path in root.rglob("*.py"):
            text = str(path)
            if "/tests/" in text or path.name.startswith("test_"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=text)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if name != "refusal" or not node.args:
                    continue
                key, kind = _literal_or_prefix(node.args[0])
                calls.append((f"{path.relative_to(_ROOT)}:{node.lineno}", key, kind))
    return calls


# A handful of call sites build their key from a variable, not a literal, so static
# AST extraction sees neither an exact string nor an f-string literal prefix. Each is
# resolved by hand against the dict or call graph that actually supplies the key, so a
# rename of the source dict/constant without updating this list fails loudly here
# rather than silently passing an unchecked key.
_DYNAMIC_KEY_SITES = {
    # celerp/services/document_lines.py: key, text = _PROTECTED_MESSAGES[why]
    "celerp/services/document_lines.py": ["line.protected_held", "line.protected_received"],
    # celerp/services/cogs_backfill.py: _outcome(key) for key in ("posted_one",
    # "posted_many") + _OPTIONAL_PARTS, key is f"{_NOTICE}.{key}" with _NOTICE a
    # module-level constant (non-literal first f-string segment)
    "celerp/services/cogs_backfill.py": [
        "notice.cogs_backfill.posted_one", "notice.cogs_backfill.posted_many",
        "notice.cogs_backfill.zero_cost", "notice.cogs_backfill.deferred",
        "notice.cogs_backfill.older_stock", "notice.cogs_backfill.errored",
        "notice.cogs_backfill.skipped",
    ],
    # default_modules/celerp-docs/celerp_docs/legacy_credit_notes.py: _outcome(key)
    # for key in ("settled", "restored", "voided", "double_credit", "open_credit",
    # "title", "body"), same _NOTICE-prefixed pattern
    "default_modules/celerp-docs/celerp_docs/legacy_credit_notes.py": [
        "notice.legacy_credit_notes.settled", "notice.legacy_credit_notes.restored",
        "notice.legacy_credit_notes.voided", "notice.legacy_credit_notes.double_credit",
        "notice.legacy_credit_notes.open_credit", "notice.legacy_credit_notes.title",
        "notice.legacy_credit_notes.body",
    ],
    # default_modules/celerp-docs/celerp_docs/routes.py: _undo_blocked(key, lead,
    # reasons) is called with a literal key, but the literal is at the call site of
    # the wrapper, one frame away from the refusal() call AST sees
    "default_modules/celerp-docs/celerp_docs/routes.py": [
        "docs.undo_return_blocked", "docs.undo_receipt_blocked",
    ],
}


def test_every_exact_refusal_key_is_in_the_catalog():
    en = _en_keys()
    missing = {
        site: key for site, key, kind in _refusal_calls()
        if kind == "exact" and key not in en
    }
    assert not missing, f"refusal() call sites naming a key absent from en.json: {missing}"


def test_every_dynamic_refusal_key_is_in_the_catalog():
    en = _en_keys()
    missing = {
        site: [key for key in keys if key not in en]
        for site, keys in _DYNAMIC_KEY_SITES.items()
        if any(key not in en for key in keys)
    }
    assert not missing, f"dynamically-built refusal() keys absent from en.json: {missing}"


def test_every_prefix_refusal_key_matches_at_least_one_catalog_key():
    """An f-string key built as f"{prefix}{suffix}" can't be resolved to an exact
    string by AST alone, but a prefix with zero matching en.json keys means the
    source of the suffix and the catalog have drifted apart - at least one key
    under the prefix must exist."""
    en = _en_keys()
    dead = {
        site: prefix for site, prefix, kind in _refusal_calls()
        if kind == "prefix" and not any(k.startswith(prefix) for k in en)
    }
    assert not dead, f"refusal() key prefixes with no matching en.json key: {dead}"


def test_no_refusal_call_site_is_unresolved():
    """Every refusal() call site is accounted for by exactly one of: an exact
    literal, a literal f-string prefix, or an explicit entry in
    _DYNAMIC_KEY_SITES. A new call site with a fully dynamic key (no literal
    prefix at all) must be added to _DYNAMIC_KEY_SITES by hand, same as the four
    existing ones, so it's checked instead of silently skipped."""
    unresolved = {
        site for site, key, kind in _refusal_calls()
        if key is None and site.split(":")[0] not in _DYNAMIC_KEY_SITES
    }
    assert not unresolved, f"refusal() call sites with no resolvable key: {unresolved}"
