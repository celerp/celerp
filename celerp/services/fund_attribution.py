# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Which fund each journal producer's lines carry, and why each emitter whose event type is not a
literal never posts a journal entry.

This is correctness data for the fund census (tests/fund_census.py), never the posting gate: no
posting path reads it, and every line is judged at the journal boundary on its content and its
actor. A producer missing here posts its lines without a fund, so they count as unrestricted,
and the census test fails naming it."""

from __future__ import annotations

# What each rule class says a producer's lines carry.
RULE_CLASSES: dict[str, str] = {
    "LG": "each line's fund; tax follows each line's own tax and the control each fund's gross (fund_legs)",
    "LG/V": "as LG; a line linked to a posted line copies that line's fund",
    "S": "the open shares of the document it settles, by fund",
    "SM": "the overlap of the note's funds with the document's open shares",
    "P": "the fund of the object it posts for",
    "V": "the funds of the legs it reverses, copied",
    "X": "nothing new: it only voids an entry",
    "U-stock": "no fund: stock and its cost are unrestricted",
    "L/U-stock": "a non-stock line's fund; stock carries no fund",
    "LD": "the document's own line funds; a line that arrived from outside carries a local user's choice",
    "O": "a local user's choice for every line authored outside the company",
    "M": "the fund the user chose on each line",
    "M/B": "the user's chosen fund, or a pooled balance split by fund",
    "B": "a pooled balance moved between accounts, split by fund",
    "CF": ("Change fund: the legs it voids posted again unchanged but for the fund the record now "
           "carries (rule LG on a document, a local user's choice on a journal), judged once over the action"),
    "FM": ("the giving and receiving funds the movement names, each against its own net assets; "
           "a year close closes each fund's results to that fund's own net assets"),
}

FUND_ATTRIBUTION: dict[str, str] = {
    "celerp.services.auto_je:_create_for_note_application": "SM",
    "celerp.services.auto_je:_post_opening_settled": "LD",
    "celerp.services.auto_je:_post_po_receipt": "L/U-stock",
    "celerp.services.auto_je:_restate_receivable": "S",
    "celerp.services.auto_je:_void_je_if_posted": "X",
    "celerp.services.auto_je:book_opening_inventory": "U-stock",
    "celerp.services.auto_je:correct_earlier_import.void": "X",
    "celerp.services.auto_je:create_for_audit_adjustment": "U-stock",
    "celerp.services.auto_je:create_for_bill_conversion": "LG",
    "celerp.services.auto_je:create_for_cn_application": "SM",
    "celerp.services.auto_je:create_for_consigned_return_bought": "U-stock",
    "celerp.services.auto_je:create_for_cost_moves": "U-stock",
    "celerp.services.auto_je:create_for_credit_note_finalized": "LG/V",
    "celerp.services.auto_je:create_for_debit_note_finalized": "LG/V",
    "celerp.services.auto_je:create_for_dn_application": "SM",
    "celerp.services.auto_je:create_for_doc_cogs_adjustment": "U-stock",
    "celerp.services.auto_je:create_for_doc_cogs_backfill": "U-stock",
    "celerp.services.auto_je:create_for_doc_finalized": "LG",
    "celerp.services.auto_je:create_for_doc_fulfilled": "U-stock",
    "celerp.services.auto_je:create_for_doc_payment": "S",
    "celerp.services.auto_je:create_for_doc_unvoided": "V",
    "celerp.services.auto_je:create_for_imported_document": "LD",
    "celerp.services.auto_je:create_for_imported_paid": "LD",
    "celerp.services.auto_je:create_for_imported_used": "LD",
    "celerp.services.auto_je:create_for_landed_capitalisation": "U-stock",
    "celerp.services.auto_je:create_for_line_adjustment": "U-stock",
    "celerp.services.auto_je:create_for_merge_reclassification": "U-stock",
    "celerp.services.auto_je:create_for_mfg_movement": "U-stock",
    "celerp.services.auto_je:create_for_online_fees": "P",
    "celerp.services.auto_je:create_for_po_receipt": "L/U-stock",
    "celerp.services.auto_je:create_for_receipt_undone": "V",
    "celerp.services.auto_je:create_for_return_received": "U-stock",
    "celerp.services.auto_je:create_for_return_undone": "U-stock",
    "celerp.services.auto_je:create_for_supplier_return": "V",
    "celerp.services.auto_je:release_credit_note_open": "V",
    "celerp.services.auto_je:reverse_imported_carriage": "V",
    "celerp.services.auto_je:reverse_return_charges": "V",
    "celerp.services.auto_je:void_for_doc_payment": "V",
    "celerp.services.auto_je:void_for_list_adjustment": "X",
    "celerp.services.auto_je:void_record_postings": "X",
    "celerp.services.auto_je:repost_record_postings": "V",
    "celerp.services.auto_je:void_landed_capitalisation": "V",
    "celerp.services.lot_origin:_move": "U-stock",
    "celerp.services.lot_origin:_normalize": "U-stock",
    "celerp.services.lot_origin:_place": "U-stock",
    "celerp.services.lot_origin:book_lot_value": "U-stock",
    "celerp.services.lot_origin:post_opening_stock_delta": "U-stock",
    "celerp.services.module_journals:post_journal": "P",
    "celerp.services.module_journals:reverse_journal": "X",
    "celerp.services.module_journals:move_journal_funds": "CF",
    "celerp.services.peer_journals:apply_firm_journal": "O",
    "celerp.services.peer_journals:receive_journal": "O",
    "celerp.services.peer_journals:_post": "O",
    "celerp.services.peer_journals:_post_client_journal": "O",
    "celerp.services.peer_journals:reverse": "V",
    "celerp.services.peer_lines:change_posted_fund": "CF",
    "celerp_accounting.funds:post_fund_movement": "FM",
    "celerp_accounting.import_service:import_journal_records": "O",
    "celerp_accounting.migration_sink:_journal_record": "O",
    "celerp_accounting.recognition:reverse_recognition": "V",
    "celerp_accounting.recognition:run_recognition": "LD",
    "celerp_accounting.routes:_void_one": "X",
    "celerp_accounting.routes:_void_reconciliation_created_je": "X",
    "celerp_accounting.routes:create_bank_account": "M",
    "celerp_accounting.routes:create_je_from_line": "M/B",
    "celerp_accounting.routes:create_manual_journal_entry": "M",
    "celerp_accounting.routes:create_transfer": "B",
    "celerp_accounting.routes:split_stmt_line": "M/B",
    "celerp_accounting.routes:write_off_difference": "M/B",
    "celerp_admin.routes:_check_uncaused_recognition_jes": "X",
    "celerp_admin.routes:_check_zero_amount_jes": "X",
    "celerp_docs.routes:delete_payment": "X",
    "celerp_docs.routes:reverse_payment_refund": "V",
    "celerp_docs.routes:void_payment": "X",
}

# Emitters whose event type is a variable, each with where its value comes from. The journal
# boundary fires only on acc.journal_entry.created exactly.
NON_JOURNAL_EMITTERS: dict[str, str] = {
    "celerp.events.engine:connector_upsert": "callers pass crm.contact.created, doc.created, item.created",
    "celerp.routers.companies:batch_import_settings": "refuses every record whose event_type is not sys.company.created",
    "celerp.services.attachments:attach_file": "FILE_ATTACHED_EVENTS map of *.file_attached types",
    "celerp.services.collection:record_binding": "constant BINDING_EVENT (crm.contact.collection_binding) on the payer contact",
    "celerp.services.collection:set_attempt": "constant ATTEMPT_EVENT (doc.collection) on the invoice",
    "celerp.services.lot_origin:_record": "lot_origin constants item.inventory_account.* and item.inventory_on_books.recorded",
    "celerp.services.lot_origin:correct_placements": "lot_origin constant sys.older_stock.corrected",
    "celerp_accounting.funds:_emit": "callers pass only fund.* literals",
    "celerp_accounting.donors:send_statements": "constant STATEMENT_SENT (crm.contact.statement_sent) on the donor contact",
    "celerp_connectors.routes:set_item_sync": "literals shop.sync.enabled / shop.sync.disabled",
    "celerp_contacts.routes:merge_contacts_service": "f-string ending .updated",
    "celerp_contacts.services:create_crm_entity": "f-string crm.<type>.created on entity crm",
    "celerp_contacts.services:import_contact_records": "refuses every record whose event_type is not crm.contact.created",
    "celerp_docs.import_service:create_imported_doc": "constant DOC_CREATED (doc.created)",
    "celerp_docs.legacy_credit_notes:_void_with_invoice": "loop over doc.* literal steps",
    "celerp_docs.received:_emit": "callers pass only received_doc.* literals",
    "celerp_docs.received:record_received": "literals received_doc.revised / received_doc.imported",
    "celerp_docs.routes:_apply_split_plan": "f-string ending .updated",
    "celerp_docs.routes:_emit_list": "callers pass only list.* literals",
    "celerp_docs.routes:_fulfill_lines_impl": "doc_event_type literals doc.fulfilled / doc.partially_fulfilled",
    "celerp_docs.routes:_reverse_whole_lines": "doc_event_type literals doc.*",
    "celerp_docs.routes:finalize_document": "doc.finalized / doc.converted_to_bill literals",
    "celerp_docs.routes:record_historical_delivery": "doc.fulfilled / doc.partially_fulfilled literals",
    "celerp_docs.routes:unvoid_doc": "doc.fulfilled / doc.partially_fulfilled literals",
    "celerp_fixed_assets.setup:_emit": "callers pass only fa.class.* literals",
    "celerp_fixed_assets.store:_emit": "callers pass only fa.* literals",
    "celerp_inventory.routes:patch_item": "event dict with event_type item.updated",
    "celerp_inventory.routes:set_item_price": "event dict with event_type item.pricing.set",
    "celerp_inventory.services:restate_item_cost": "callers pass item.updated / item.cost_adjusted",
    "celerp_inventory.services:update_item_from_connector": "event dict of item.* types",
    "celerp_inventory.services:write_import_batch": "refuses every primary record whose event_type is not item.created; follow-ups are item.patched",
    "celerp_manufacturing.movements:_Op.emit": "manufacturing movement callers pass item.* literals",
}


# How each producer that voids or reverses an entry finds the entry it undoes. Change fund voids an
# entry and posts it again under a new id, naming the copy in ``replaced_by``, so an undo reading a
# fixed id would leave the copy standing. "current_entry": it resolves the id through
# auto_je.current_entry; "family": it enumerates the record's live family, copies included; any
# other value says why Change fund never replaces what it undoes. Checked by
# tests/test_fund_attribution.py against the source.
UNDO_LIVE_ENTRY: dict[str, str] = {
    "celerp.services.auto_je:book_opening_inventory": "never: an entry of its own no outside record carries, so Change fund never replaces it",
    "celerp.services.auto_je:correct_earlier_import.void": "family",
    "celerp.services.auto_je:create_for_doc_unvoided": "family",
    "celerp.services.auto_je:create_for_receipt_undone": "family",
    "celerp.services.auto_je:create_for_supplier_return": "family",
    "celerp.services.auto_je:release_credit_note_open": "never: a payment or note entry; Change fund refuses a settled document",
    "celerp.services.auto_je:restore_return_charges": "family",
    "celerp.services.auto_je:repost_record_postings": "family",
    "celerp.services.auto_je:reverse_imported_carriage": "never: what the opening balances hold; Change fund refuses a document they hold",
    "celerp.services.auto_je:reverse_return_charges": "family",
    "celerp.services.auto_je:take_back_imported_reversals": "never: what the opening balances hold; Change fund refuses a document they hold",
    "celerp.services.auto_je:void_credit_note_payment_rate": "never: a payment or note entry; Change fund refuses a settled document",
    "celerp.services.auto_je:void_for_doc_finalized": "family",
    "celerp.services.auto_je:void_for_doc_payment": "never: a payment or note entry; Change fund refuses a settled document",
    "celerp.services.auto_je:void_for_list_adjustment": "never: an entry of its own no outside record carries, so Change fund never replaces it",
    "celerp.services.auto_je:void_for_merge_reclassification": "never: an entry of its own no outside record carries, so Change fund never replaces it",
    "celerp.services.auto_je:void_imported_used": "never: what the opening balances hold; Change fund refuses a document they hold",
    "celerp.services.auto_je:void_landed_capitalisation": "family",
    "celerp.services.auto_je:void_record_postings": "family",
    "celerp.services.auto_je:void_supplier_return": "current_entry",
    "celerp.services.module_journals:reverse_journal": "current_entry",
    "celerp.services.module_journals:move_journal_funds": "current_entry",
    "celerp.services.peer_journals:apply_firm_journal": "current_entry",
    "celerp.services.peer_journals:receive_journal": "current_entry",
    "celerp.services.peer_journals:reverse": "current_entry",
    "celerp.services.peer_lines:change_posted_fund": "family",
    "celerp_accounting.recognition:reverse_recognition": "family",
    "celerp_accounting.routes:_void_one": "never: voids the entry the user opened; a replaced entry is void already and refused as not posted",
    "celerp_accounting.routes:_void_reconciliation_created_je": "never: an entry of its own no outside record carries, so Change fund never replaces it",
    "celerp_admin.routes:_check_uncaused_recognition_jes": "never: sweeps entries by their own posted state",
    "celerp_admin.routes:_check_zero_amount_jes": "never: sweeps entries by their own posted state",
    "celerp_docs.routes:delete_payment": "never: a payment or note entry; Change fund refuses a settled document",
    "celerp_docs.routes:reverse_payment_refund": "never: a payment or note entry; Change fund refuses a settled document",
    "celerp_docs.routes:void_payment": "never: a payment or note entry; Change fund refuses a settled document",
    "celerp_inventory.routes:undo_import_batch": "never: an entry of its own no outside record carries, so Change fund never replaces it",
}


# Operations that void or reverse an entry and post one in the same call, each with its verdict
# against the fund rules: how the entries it writes together are judged so that no value moves
# between funds without a judge. Checked by tests/test_fund_attribution.py against the source.
ONE_ACTION = "one fund_change_action, judged once by judge_fund_change over every entry it voids or posts"
NETS_TO_ZERO = ("what it posts copies the funds of the lines it voids or reverses (rules X and V), "
                "so what it writes nets to zero on every fund")
EACH_ENTRY = ("a void removes a whole entry (rule X, zero on every fund) and each new entry is judged "
              "on its own by check_fund_rules, a fresh posting by the user who makes it")
MOVEMENT = "a fund movement or its reversal: one entry, judged by its type's template and cap"

MULTI_ENTRY_OPERATIONS: dict[str, str] = {
    "celerp.services.auto_je:book_opening_inventory": EACH_ENTRY,
    "celerp.services.auto_je:correct_earlier_import": EACH_ENTRY,
    "celerp.services.auto_je:create_for_doc_unvoided": NETS_TO_ZERO,
    "celerp.services.auto_je:restore_return_charges": NETS_TO_ZERO,
    "celerp.services.auto_je:take_back_imported_reversals": NETS_TO_ZERO,
    "celerp.services.auto_je:void_credit_note_payment_rate": NETS_TO_ZERO,
    "celerp.services.auto_je:void_for_doc_finalized": EACH_ENTRY,
    "celerp.services.auto_je:void_for_doc_voided": NETS_TO_ZERO,
    "celerp.services.auto_je:void_for_merge_reclassification": NETS_TO_ZERO,
    "celerp.services.auto_je:void_imported_used": NETS_TO_ZERO,
    "celerp.services.auto_je:void_landed_capitalisation": EACH_ENTRY,
    "celerp.services.auto_je:void_supplier_return": NETS_TO_ZERO,
    "celerp.services.lot_origin:_normalize": EACH_ENTRY,
    "celerp.services.lot_origin:_open": EACH_ENTRY,
    "celerp.services.module_journals:move_journal_funds": ONE_ACTION,
    "celerp.services.peer_journals:apply_firm_journal": NETS_TO_ZERO,
    "celerp.services.peer_journals:receive_journal": NETS_TO_ZERO,
    "celerp.services.peer_journals:reverse": NETS_TO_ZERO,
    "celerp.services.peer_lines:change_posted_fund": ONE_ACTION,
    "celerp_accounting.funds:post_fund_movement": MOVEMENT,
    "celerp_accounting.recognition:reverse_recognition": NETS_TO_ZERO,
    "celerp_docs.routes:delete_payment": EACH_ENTRY,
    "celerp_docs.routes:finalize_document": EACH_ENTRY,
    "celerp_docs.routes:revert_doc_to_draft": EACH_ENTRY,
    "celerp_docs.routes:settle_imported_credit": EACH_ENTRY,
    "celerp_docs.routes:undo_receive_record": EACH_ENTRY,
    "celerp_docs.routes:void_payment": NETS_TO_ZERO,
    "celerp_fixed_assets.corrections:undo": NETS_TO_ZERO,
    "celerp_fixed_assets.runs:reverse": NETS_TO_ZERO,
    "celerp_inventory.routes:undo_import_batch": EACH_ENTRY,
}


# Every function that writes a fund under a fund key, with its verdict: a record that copies a
# fund from a document's posted lines names the doc_posting_dependents handler that moves it when
# Change fund posts those lines on new funds; anything else says why Change fund leaves it alone.
# Checked by tests/test_fund_attribution.py against the source and the slot's handlers.
DEPENDENT = "moved by the doc_posting_dependents handler "
JOURNAL_LINE = "a journal line or a journal's own data: Change fund voids the entry and posts it again"
OWN_CHOICE = "the record's own fund choice: Change fund rewrites it through require_fund_choices"
STANDING = "a standing default: a setting, not a copy; each record stores its own choice"
SETTLED = "a payment's split by fund: Change fund refuses a document a payment or note settles"
NOTE_LINES = "a linked note's lines, copied when the note is made: the note keeps its own funds"
READ = "reads the posted lines to show or choose from them; stores nothing"
DRAFT = "a draft line's starting fund, the user's to change; finalizing checks it against its source (rule P)"
FUND_COPY_REASONS = frozenset({JOURNAL_LINE, OWN_CHOICE, STANDING, SETTLED, NOTE_LINES, READ, DRAFT})
_ASSETS = DEPENDENT + "celerp_fixed_assets.fundchange:dependents"
_SCHEDULES = DEPENDENT + "celerp_accounting.recognition:dependents"

FUND_COPIES: dict[str, str] = {
    "celerp.events.engine:emit_event": OWN_CHOICE,
    "celerp.services.auto_je:create_for_bill_conversion": JOURNAL_LINE,
    "celerp.services.auto_je:create_for_doc_payment": SETTLED,
    "celerp.services.journal_accounts:fund_legs": JOURNAL_LINE,
    "celerp.services.journal_accounts:split_lines_by_fund": JOURNAL_LINE,
    "celerp.services.peer_lines:posted_line_funds": NOTE_LINES,
    "celerp.services.peer_lines:require_fund_choices": OWN_CHOICE,
    "celerp.services.recognition:schedule_records": _SCHEDULES,
    "ui.routes.csv_import:import_numbered": OWN_CHOICE,
    "ui.routes.docs_import:setup_routes.docs_import_confirm": OWN_CHOICE,
    "celerp_accounting.fund_reports:check_fund_reconciliation": READ,
    "celerp_accounting.funds:_default_items": STANDING,
    "celerp_accounting.funds:_leg_lines.line": JOURNAL_LINE,
    "celerp_accounting.funds:post_fund_movement": JOURNAL_LINE,
    "celerp_accounting.funds:set_connection_default": STANDING,
    "celerp_accounting.recognition:_leg_keys": JOURNAL_LINE,
    "celerp_accounting.recognition:dependents": _SCHEDULES,
    "celerp_accounting.routes:batch_import_accounting": OWN_CHOICE,
    "celerp_accounting.routes:close_fiscal_year": JOURNAL_LINE,
    "celerp_docs.routes:apply_doc_payment": SETTLED,
    "celerp_docs.routes:batch_import_docs": OWN_CHOICE,
    "celerp_fixed_assets.additions:add.apply": _ASSETS,
    "celerp_fixed_assets.additions:place_in_service.apply": _ASSETS,
    "celerp_fixed_assets.fixaccount:_split": JOURNAL_LINE,
    "celerp_fixed_assets.fixaccount:fix_account.plan": JOURNAL_LINE,
    "celerp_fixed_assets.fundchange:_on": JOURNAL_LINE,
    "celerp_fixed_assets.fundchange:dependents": _ASSETS,
    "celerp_fixed_assets.operations:_post": JOURNAL_LINE,
    "celerp_fixed_assets.operations:_reclass": JOURNAL_LINE,
    "celerp_fixed_assets.operations:register.apply": _ASSETS,
    "celerp_fixed_assets.runs:_lines": JOURNAL_LINE,
    "celerp_fixed_assets.sales:_draft_invoice": DRAFT,
    "celerp_fixed_assets.sources:_bill_lines": READ,
    "celerp_fixed_assets.sources:_manual_lines": READ,
    "ui.routes.documents:_submitted_fund_allocation": SETTLED,
}
