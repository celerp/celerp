# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The census of every line writer in the app's source and the closed line-key table over it.

A line writer creates or replaces document lines (``line_items``) or journal lines
(``entries``), counted at its innermost function. Two kinds:

- EMIT: an ``emit_event`` call that can write lines: its ``event_type`` is not a literal (or is a
  ``**`` spread), or is ``acc.journal_entry.created`` or a ``doc.*`` or ``list.*`` event, unless its
  data is a dict literal with none of ``line_items``, ``entries`` and ``fields_changed``.
- SEND: a function that builds a ``line_items`` or ``entries`` value and hands it to the API
  client (``api.*``), so its write lands in the EMIT row of the route it calls.

Every writer has one row in ROWS (path::function -> class, reason), and every class has one cell
per fund key in CELLS (rules LK1 to LK7 of the funds design). HELPERS are the functions the funds
design adds that write through a row rather than emitting themselves; PENDING are the writers it
adds that are not built yet, so a new writer of that name must get its row."""

from __future__ import annotations

import ast
import dataclasses

from fund_census import sources
from shipped_sources import REPO

LINE_KEYS = frozenset({"line_items", "entries"})
ARRIVAL_CLASSES = frozenset({"ARRIVAL", "ARRIVAL-JE"})

_KEPT = "omitted keeps the stored value (LK1)"
_GRANT = "a present, different value only under a grant (LK3)"
CELLS = {  # class -> (origin, fund_choice, fund_id)
    "ARRIVAL": ("LK2a: added lines get the emit's origin=; kept lines LK1; a present, different value refused (LK2)",
                f"inbound value refused naming the key (LK7); kept lines LK1; {_GRANT}; cleared only by LK5",
                "inbound value refused (LK7); kept lines LK1; LK3; LK5"),
    "ARRIVAL-JE": ("LK6: the emit's origin= is stamped on every entry line",
                   "inbound value refused (LK7); written only under a grant from require_fund_choices (LK3)",
                   "inbound value refused (LK7); on a marked line only with its granted choice (LK3), checked at posting"),
    "LOCAL": ("added lines carry none; a body adding one refused (LK2); kept lines LK1",
              f"{_KEPT}; {_GRANT}; cleared only by LK5",
              "unmarked line: as the signed-in writer sends it (LK4); marked line: LK1, LK3, LK5"),
    "PATCH": ("added lines get the keyword-only origin= an arrival caller passes (LK2a), a copy's source origin "
              "under a copy grant (LK2b), otherwise none (LK2)",
              f"{_KEPT}; {_GRANT}; cleared only by LK5, with the source's reason when the write carries origin=",
              "unmarked line: as the signed-in writer sends it (LK4); marked line: LK1, LK3, LK5"),
    "COPY": ("LK2b: each copy takes its source line's origin under a granted_from_stored grant; kept lines LK1",
             "LK2b: copied with the origin under the same grant; kept lines LK1, LK3, LK5",
             "LK2b: copied for marked or linked lines; unmarked lines LK4"),
    "LOCAL-JE": ("none on any line; a body carrying origin refused (LK2)",
                 "none; a body carrying it refused (LK3)",
                 "as the signed-in user sends it (LK4)"),
    "DERIVED-JE": ("LK2b: each leg takes the origin of the document lines it is built from",
                   "LK2b: the stored choice of those lines, under a granted_from_stored grant",
                   "the leg's fund (rules LG, LD); on a marked leg equal to its choice, checked at posting"),
    "COPY-JE": ("LK2b: copy_posted_lines copies the posted line's origin (rule V)",
                "LK2b: copied, or the posted fund or unrestricted for a line posted before funds were in use",
                "copied from the posted line (rule V)"),
    "CHOICE": ("never written; a present, different value refused (LK2)",
               "the one writer: require_fund_choices builds the grant (LK3)",
               "written with the choice under the same grant (LK3)"),
    "SEND": ("sends none; the route it lands in keeps the stored value (LK1)",
             "sends none (LK1)",
             "sends it only for unmarked lines (the Fund column, LK4); marked lines omit it (LK1)"),
    "NO-LINE-CHANGE": ("writes no lines (status, payment, number or date); LK1 would apply to any it did",) * 3,
    "READ": ("builds lines for display only; writes nothing",) * 3,
    "NON-LINE": ("the event is not a document, List or journal write",) * 3,
}

_CORE, _ACC, _DOCS = "celerp/services", "default_modules/celerp-accounting/celerp_accounting", \
    "default_modules/celerp-docs/celerp_docs"
_ROUTES, _UI = f"{_DOCS}/routes.py", "ui/routes/documents.py"


def _many(path: str, names: str, cls: str, why: str = "") -> dict[str, tuple[str, str]]:
    return {f"{path}::{name}": (cls, why) for name in names.split()}


ROWS: dict[str, tuple[str, str]] = {
    "celerp/events/engine.py::connector_upsert": (
        "ARRIVAL", "required keyword origin: accounting_system for QuickBooks and Xero, none for a shop order"),
    **_many("celerp/routers/companies.py", "batch_import_settings", "NON-LINE"),
    **_many(f"{_CORE}/attachments.py", "attach_file", "NON-LINE"),
    f"{_CORE}/auto_je.py::_emit_auto_posted_je": ("DERIVED-JE", "every document posting"),
    **_many(f"{_CORE}/lot_origin.py", "_record _place _normalize _move correct_placements", "NON-LINE"),
    f"{_CORE}/peer_journals.py::_post_client_journal": (
        "ARRIVAL-JE", "client: a client's journal at the firm, from receive_journal and post_held"),
    f"{_CORE}/peer_journals.py::_post": (
        "ARRIVAL-JE", "accountant: the firm's journal at the client, from apply_firm_journal and post_held"),
    f"{_CORE}/peer_journals.py::reverse": ("COPY-JE", "Reverse from accountant"),
    f"{_ACC}/funds.py::_emit": ("NON-LINE", ""),
    f"{_ACC}/funds.py::post_fund_movement": ("LOCAL-JE", "movement templates; never on a marked line"),
    f"{_ACC}/import_service.py::import_journal_records": (
        "ARRIVAL-JE", "file_import (batch route) or migration (sink)"),
    **_many(f"{_ACC}/routes.py", "create_manual_journal_entry create_bank_account create_transfer create_je_from_line "
            "split_stmt_line write_off_difference", "LOCAL-JE", "a user's own entry"),
    "default_modules/celerp-admin/celerp_admin/routes.py::_check_inverted_doc_dates": (
        "LOCAL", "the integrity doctor correcting dates on stored lines"),
    **_many("default_modules/celerp-connectors/celerp_connectors/routes.py", "set_item_sync", "NON-LINE"),
    **_many("default_modules/celerp-contacts/celerp_contacts/routes.py", "merge_contacts_service", "NON-LINE"),
    **_many("default_modules/celerp-contacts/celerp_contacts/services.py", "import_contact_records create_crm_entity",
            "NON-LINE"),
    f"{_DOCS}/consignment_buy.py::buy_consignment": ("COPY", "vendor bill from an issued consignment"),
    f"{_DOCS}/customs_freeze.py::freeze_issued_customs_lists": ("LOCAL", "customs snapshot on stored List lines"),
    **_many(f"{_DOCS}/doc_service.py", "_record_woocommerce_hold set_woocommerce_order_reconciled "
            "upsert_order_from_woocommerce", "LOCAL", "the company's own shop order"),
    f"{_DOCS}/import_service.py::create_imported_doc": (
        "ARRIVAL", "file_import, migration or connected_company (receive_document)"),
    f"{_DOCS}/imported_cutover.py::_correct": ("LOCAL", "one-time correction of stored imported documents"),
    f"{_DOCS}/legacy_credit_notes.py::_void_with_invoice": ("NO-LINE-CHANGE", "legacy credit note void"),
    f"{_DOCS}/legacy_credit_notes.py::_post": ("LOCAL", "legacy credit note settlement on stored lines"),
    **_many(f"{_DOCS}/received.py", "record_received _emit", "NON-LINE"),
    f"{_ROUTES}::create_doc_record": (
        "ARRIVAL", "shared_document when received.book passes origin=; otherwise LOCAL, and a credit note line "
        "with source_line_id copies the posted invoice line (LK2b)"),
    f"{_ROUTES}::write_fund_choices": ("CHOICE", "the fund-choices route and the holds, under require_fund_choices"),
    f"{_ROUTES}::write_doc_patch": (
        "PATCH", "the document PATCH; received.update_draft and the document import pass origin="),
    f"{_ROUTES}::_emit_list": ("PATCH", "List create, update and duplicate_list (a duplicate copies lines, LK2b)"),
    **_many(_ROUTES, "mark_doc_sent void_document close_doc reopen_doc revert_doc_to_draft renumber_doc unvoid_doc "
            "apply_doc_payment apply_payment_refund return_consignment_items", "NO-LINE-CHANGE"),
    **_many(_ROUTES, "finalize_document emit_credit_settlement set_memo_tax_rate record_historical_delivery "
            "reprice_doc _apply_split_plan _fulfill_lines_impl _reverse_whole_lines", "LOCAL",
            "writes stored lines; the posting check runs in the same transaction"),
    **_many(_ROUTES, "_supplier_credit_for_return convert_doc convert_list", "COPY", "a document made from another"),
    **_many(_ROUTES, "import_list batch_import_lists", "ARRIVAL",
            "file_import: a List from a file; the choice is made on the document a conversion makes"),
    **_many("default_modules/celerp-fixed-assets/celerp_fixed_assets/demo.py", "seed_demo_assets "
            "_follow_books_currency", "LOCAL", "demo data of an empty company"),
    "default_modules/celerp-fixed-assets/celerp_fixed_assets/sales.py::_draft": (
        "LOCAL", "fixed-asset sale draft from this company's assets"),
    **_many("default_modules/celerp-fixed-assets/celerp_fixed_assets/setup.py", "_emit", "NON-LINE"),
    **_many("default_modules/celerp-fixed-assets/celerp_fixed_assets/store.py", "_emit", "NON-LINE"),
    **_many("default_modules/celerp-inventory/celerp_inventory/routes.py", "patch_item set_item_price", "NON-LINE"),
    **_many("default_modules/celerp-inventory/celerp_inventory/services.py", "restate_item_cost "
            "update_item_from_connector write_import_batch", "NON-LINE"),
    **_many("default_modules/celerp-manufacturing/celerp_manufacturing/movements.py", "emit", "NON-LINE"),
    "default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::write_agreement": (
        "LOCAL", "the agreement's own template lines"),
    "default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::_bill": (
        "COPY", "template lines copied into the period's invoice or purchase order (claim_period)"),
    "ui/routes/accounting.py::journal_new_submit": ("SEND", "a user's journal form"),
    **_many(_UI, "create_doc_from_items create_list_from_items", "SEND", "this company's items"),
    **_many(_UI, "add_items_to_doc add_items_to_list", "SEND", "stored lines plus this company's items"),
    f"{_UI}::doc_items_import_csv": ("SEND", "stored lines plus items picked from the user's own file"),
    f"{_UI}::doc_li_field_patch": ("SEND", "one line field edit on the stored lines"),
    f"{_UI}::save_doc_lines": ("SEND", "the line editor; sends line_id for every kept line"),
    f"{_UI}::doc_action": (
        "SEND", "create credit note: sends source_line_id per invoice line and strips the fund keys"),
    f"{_UI}::list_detail": ("READ", "List page"),
    "ui/routes/inventory.py::send_to_action": ("SEND", "stored lines plus items"),
    "ui/routes/subscriptions_import.py::subs_import_confirm": ("SEND", "agreement import sends no lines"),
}

HELPERS: dict[str, tuple[str, str]] = {
    f"{_CORE}/peer_lines.py::require_fund_choices": ("CHOICE", "builds the one grant that writes fund choices"),
    f"{_CORE}/peer_lines.py::copy_posted_lines": ("COPY-JE", "reversals, restores and linked credit lines"),
    f"{_CORE}/peer_journals.py::post_held": ("ARRIVAL-JE", "posts a held journal through _post or _post_client_journal"),
    f"{_CORE}/peer_documents.py::post_held_document": ("ARRIVAL", "posts a held document through create_imported_doc"),
    f"{_ACC}/routes.py::reverse_year_close": ("COPY-JE", "reversal of a year close"),
    "default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::claim_period": (
        "COPY", "bills a period through _bill"),
}

PENDING = {  # function -> (class, what it will write)
    "run_recognition": ("DERIVED-JE", "schedule legs from the deferred line"),
    "reverse_recognition": ("COPY-JE", "reversal of a recognition run"),
    "post_dispute_adjustment": ("DERIVED-JE", "a payment dispute on the receipt's split"),
    "amend": ("COPY", "a seat change's credit note and debit invoice copy the template lines"),
}


@dataclasses.dataclass(frozen=True)
class Writer:
    kind: str  # EMIT or SEND
    key: str  # path::function
    line: int
    event: str  # the event_type, or the API calls of a SEND
    origin: bool  # the emit passes origin= with a value other than None


def _can_write_lines(event: ast.AST | None, data: ast.AST | None, spread: bool) -> bool:
    if event is None:
        return spread
    if not isinstance(event, ast.Constant):
        return True
    value = str(event.value)
    if value != "acc.journal_entry.created" and value.split(".")[0] not in ("doc", "list"):
        return False
    if isinstance(data, ast.Dict) and None not in data.keys:
        return any(isinstance(k, ast.Constant) and k.value in LINE_KEYS | {"fields_changed"} for k in data.keys)
    return True


def _own_nodes(fn: ast.AST):
    """The nodes of ``fn``'s own body, not of functions nested in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield node
            stack.extend(ast.iter_child_nodes(node))


def _called(call: ast.Call) -> str:
    f = call.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""


def _passes_origin(call: ast.Call) -> bool:
    value = next((kw.value for kw in call.keywords if kw.arg == "origin"), None)
    return value is not None and not (isinstance(value, ast.Constant) and value.value is None)


def census() -> tuple[list[Writer], set[str], list[str]]:
    """(every line writer, every function defined as path::function, every connector_upsert
    call that passes no origin= keyword, as path:line)."""
    writers: list[Writer] = []
    defined: set[str] = set()
    bare_upserts: list[str] = []
    for _module, path in sources():
        rel = path.relative_to(REPO).as_posix()
        for fn in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(fn, ast.Call) and _called(fn) == "connector_upsert" and \
                    not any(kw.arg == "origin" for kw in fn.keywords):
                bare_upserts.append(f"{rel}:{fn.lineno}")
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            key = f"{rel}::{fn.name}"
            defined.add(key)
            builds, calls, emits = False, set(), []
            for node in _own_nodes(fn):
                if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value in LINE_KEYS
                                                      for k in node.keys):
                    builds = True
                if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store) and \
                        isinstance(node.slice, ast.Constant) and node.slice.value in LINE_KEYS:
                    builds = True
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and \
                        node.func.value.id in ("api", "_api"):
                    calls.add(node.func.attr)
                if _called(node) != "emit_event":
                    continue
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                event = kw.get("event_type")
                spread = any(k.arg is None for k in node.keywords)
                if _can_write_lines(event, kw.get("data"), spread):
                    emits.append(Writer("EMIT", key, node.lineno, "**spread" if event is None else ast.unparse(event),
                                        _passes_origin(node)))
            writers += sorted(emits, key=lambda w: w.line)
            if builds and calls and not emits:
                writers.append(Writer("SEND", key, fn.lineno, ",".join(sorted(calls)), False))
    return writers, defined, bare_upserts


def table(writers: list[Writer]) -> list[str]:
    """The closed line-key table: one row per writer, helper and pending writer, each with its
    class and the cell its class gives each fund key."""
    rows = ["| # | Kind | Writer | event_type or calls | Class | origin | fund_choice | fund_id |",
            "|---|---|---|---|---|---|---|---|"]
    entries = [(w.kind, f"{w.key} (line {w.line})", w.event, *ROWS[w.key]) for w in writers]
    entries += [("HELPER", key, "-", *row) for key, row in HELPERS.items()]
    entries += [("PENDING", name, "-", *row) for name, row in PENDING.items()]
    for n, (kind, where, event, cls, why) in enumerate(entries, 1):
        rows.append(f"| L{n} | {kind} | `{where}` | `{event}` | {cls}{': ' + why if why else ''} | "
                    + " | ".join(CELLS[cls]) + " |")
    return rows
