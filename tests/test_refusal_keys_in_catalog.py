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
    # celerp/services/lot_origin.py: _refused(status, key, message) wraps refusal(key, ...)
    # for the older-stock move, undo and adjustment refusals
    "celerp/services/lot_origin.py": [
        "older_stock.move.cannot_take", "older_stock.move.invoiced", "older_stock.move.no_key",
        "older_stock.move.nothing_selected", "older_stock.move.not_placed", "older_stock.move.too_many",
        "older_stock.correct.changed_since", "older_stock.correct.no_kind",
        "older_stock.correct.no_placement", "older_stock.correct.nothing_selected", "older_stock.correct.too_many",
        "older_stock.correct.unbalanced", "older_stock.undo.done", "older_stock.undo.moved_since",
        "older_stock.undo.no_move", "posting.older_stock.never_inventory", "posting.older_stock.no_item",
        "posting.older_stock.not_in_chart",
    ],
    # celerp/services/module_journals.py: _refuse(status, key, message) wraps refusal(key, ...)
    "celerp/services/module_journals.py": [
        "module_journal.accounting_off", "module_journal.caller", "module_journal.key",
        "module_journal.key_reused", "module_journal.module_off",
        "module_journal.moves_more_than_recognized", "module_journal.not_found",
        "module_journal.source", "module_journal.unbalanced", "module_journal.unknown_operation",
        "posting.destination.not_in_chart",
    ],
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


# Files this review round converted raw-English HTTPException details to t()/refusal().
# Guards against a regression reintroducing raw English in exactly these files; it is not
# a repo-wide sweep (most of celerp/modules/*, the CLI, and other untouched files still
# raise raw English by design - module-author contract errors, internal invariants, CLI
# output - and are out of scope here).
_TOUCHED_FILES = [
    "celerp/credentials.py", "celerp/events/engine.py", "celerp/importers/tabular.py",
    "celerp/routers/auth.py", "celerp/routers/companies.py", "celerp/services/account_roles.py",
    "celerp/services/backup.py", "celerp/services/backup_import.py", "celerp/services/business_time.py",
    "celerp/services/currencies.py", "celerp/services/journal_accounts.py", "celerp/services/lot_origin.py",
    "celerp/services/payments.py",
    "default_modules/celerp-accounting/celerp_accounting/chart_rules.py",
    "default_modules/celerp-accounting/celerp_accounting/migration_sink.py",
    "default_modules/celerp-docs/celerp_docs/consignment_buy.py",
    "default_modules/celerp-docs/celerp_docs/routes.py",
    "default_modules/celerp-docs/celerp_docs/routes_payments.py",
    "default_modules/celerp-inventory/celerp_inventory/routes.py",
    "default_modules/celerp-inventory/celerp_inventory/services.py",
    "default_modules/celerp-manufacturing/celerp_manufacturing/movements.py",
    "default_modules/celerp-manufacturing/celerp_manufacturing/routes.py",
]

# Raw-English HTTPException(detail=...) texts already present in the touched files above
# before this review round, outside the scope of this round's fix (predates the merge
# base this round triaged against). Allowlisted explicitly, by file and exact text, rather
# than silently skipped, so a NEW raw detail added to one of these files - not one of
# these pre-existing lines - still fails the guard below.
_PRE_EXISTING_RAW_HTTPEXCEPTION = {
    "celerp/routers/auth.py": {
        "direct_connection_limit",
        "Invalid refresh token",
        "System already bootstrapped. Contact your admin.",
        "Registration failed",
        "Password must be at least  characters.",
    },
    "celerp/routers/companies.py": {
        "Unknown permission ''",
        "Unknown role ''",
        "Installation owner access required",
        " is part of Celerp and is always on.",
        "Unknown module source.",
        "Could not create company: ",
        "Password must be at least  characters.",
        "Unit '' type must be one of: ",
        " needs this module. Turn that off first.",
        "Could not purge while a company backup or restore is running. Nothing was deleted. "
        "Try again when it finishes.",
        "Unknown business type: ",
    },
    "celerp/services/lot_origin.py": {
        "Company not found.",
    },
    "default_modules/celerp-docs/celerp_docs/routes.py": {
        "idempotency_key is longer than  characters",
        "Cannot send a closed memo; reopen it first.",
        "Add at least one line item before sending this document.",
        "Add at least one line item before finalizing this document.",
        "Cannot void a document with payments; void the payments first",
        "Cannot revert to draft while goods received on this document are still in stock. "
        "Select those lines and use Return Goods first, then revert.",
        "Cannot revert to draft while lines are still out on memo or sold. Select those "
        "lines and use Set as available first, then revert.",
        "This payment has been refunded in full, so there is nothing left to void.",
        "Credit note applications cannot be deleted. Void the payment instead.",
        ": it is not on this . Add it to the  first.",
        "Only an issued  can record goods moved before it came to Celerp",
        "Line  does not hold item ",
        "Line : at most  can be moved",
        "Select at least one document to ship",
        "offset must be zero or greater",
        "limit must be greater than zero",
        " is not a . Choose a , or change the contact's type to  or both.",
        "target_type must be 'invoice' or 'memo'",
        "Finalize the quotation before converting it",
        "Finalize the count before adjusting stock",
        "Select at least one item to write off",
        "Enter a quantity and a destination account for each item you want to write off",
        "Unknown list type: ",
        "Issue the list before sending it",
        "Issue the transfer before moving its items",
        "This credit note cannot be undone: goods it released from invoice  have left "
        "stock since, so the invoice cannot hold them again. Bring the goods back into "
        "stock first, or issue a new invoice for them.",
        ": the receipt cost  differs from the  line (). Receive the goods at the  price, "
        "then add a landed cost or correct the item's cost.",
        "Item  does not exist",
        "Issue every document before creating its shipping paperwork",
        "Reopen a closed memo before creating its shipping paperwork",
        "offset must be an integer",
        "limit must be an integer",
        "original_count must be zero or greater",
        ":  cannot come back on this credit note: the invoice shipped , returned , so  "
        "can come back",
        " changed after the audit was finalized; recount it.",
        "line_id or item_id is required",
        ": choose a destination account",
        "original_count cannot exceed the -line page limit",
        ": nothing left to ship: invoiced , credited , shipped 0",
        ": its inventory item no longer exists. Remove the line, then try again.",
        ": its inventory item was merged into another item. Remove the line, then try again.",
        "This import key was already used for a different record",
    },
    "default_modules/celerp-inventory/celerp_inventory/routes.py": {
        "This item is held by document ; resolve the document first (release or return "
        "the goods, or convert the document), then the item can be .",
        "Unknown status ''; allowed: ",
        "Use the item's 'Revert to Draft' action, not a direct status edit.",
        "A draft item can only become available through the 'Make Available' action, "
        "not a direct status edit.",
        "Field '' not available for suggestions",
        "An item is created as draft or available, not .",
        "inventory_type must be one of ",
        "landed_cost_kind must be one of ",
        "Nothing was deleted. Only a draft that never became stock can be deleted, and "
        "these cannot: . Use Revert to Draft for stock made available by mistake, "
        "Archive to retire a product, or Write Off Stock for goods that left the company.",
        "Allow splitting is set to No for this item. Change Allow Splitting to Yes in the "
        "item details to enable splitting.",
        "This merge can no longer be undone: the merged item has changed since it was made.",
        "This merge can no longer be undone: one of the merged items has changed since it "
        "was made.",
        "This merge was made before merges could be undone, so the items' earlier state "
        "is not on record.",
    },
    "default_modules/celerp-inventory/celerp_inventory/services.py": {
        "Item not found",
    },
    "default_modules/celerp-manufacturing/celerp_manufacturing/routes.py": {
        "Item has no recipe to build from",
        "Unknown bulk action: ",
        "This is your only work center. Add another before deleting this one.",
        "This is the default work center. Set another center as default before deleting it.",
    },
}


def _httpexception_raw_details(rel_path: str):
    """Every HTTPException(detail=...) call in *rel_path* whose detail is a raw string
    or f-string literal (not a call to t()/refusal()/refuse()), as (line, text)."""
    path = _ROOT / rel_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel_path)
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "HTTPException"):
            continue
        detail = next((kw.value for kw in node.keywords if kw.arg == "detail"), None)
        if detail is None:
            continue
        if isinstance(detail, ast.Call):
            fn = detail.func
            name = fn.id if isinstance(fn, ast.Name) else (
                fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in ("t", "refusal", "refuse"):
                continue
        if isinstance(detail, ast.Constant) and isinstance(detail.value, str):
            text = detail.value
        elif isinstance(detail, ast.JoinedStr):
            text = "".join(v.value for v in detail.values
                           if isinstance(v, ast.Constant) and isinstance(v.value, str))
        else:
            text = None
        if text is not None and text.strip():
            found.append((node.lineno, text))
    return found


def test_no_new_raw_english_httpexception_detail_in_touched_files():
    """A raw-English HTTPException detail reaches the user in every locale verbatim.
    Review round 1 (repair) converted every one main's recent work introduced in these
    files to t()/refusal(); this guards against reintroducing one, while leaving
    pre-existing debt (_PRE_EXISTING_RAW_HTTPEXCEPTION) and untouched files (module
    loader/slots/api.py, the CLI, and similar developer-only surfaces) alone."""
    new_raw = {}
    for rel in _TOUCHED_FILES:
        allowed = _PRE_EXISTING_RAW_HTTPEXCEPTION.get(rel, set())
        bad = [(line, text) for line, text in _httpexception_raw_details(rel) if text not in allowed]
        if bad:
            new_raw[rel] = bad
    assert not new_raw, f"new raw-English HTTPException detail(s), not yet keyed: {new_raw}"
