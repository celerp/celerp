# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

from enum import StrEnum


class EventType(StrEnum):
    # Items
    ITEM_CREATED = "item.created"
    ITEM_SNAPSHOT = "item.snapshot"
    ITEM_UPDATED = "item.updated"
    ITEM_PATCHED = "item.patched"
    ITEM_PRICING_SET = "item.pricing.set"
    ITEM_STATUS_SET = "item.status.set"
    ITEM_TRANSFERRED = "item.transferred"
    ITEM_QUANTITY_ADJUSTED = "item.quantity.adjusted"
    ITEM_EXPIRED = "item.expired"
    ITEM_DISPOSED = "item.disposed"
    ITEM_SPLIT = "item.split"
    ITEM_MERGED = "item.merged"
    ITEM_CONSUMED = "item.consumed"
    ITEM_PRODUCED = "item.produced"
    ITEM_RESERVED = "item.reserved"
    ITEM_UNRESERVED = "item.unreserved"

    # CRM
    CRM_CONTACT_CREATED = "crm.contact.created"
    CRM_CONTACT_UPDATED = "crm.contact.updated"
    CRM_CONTACT_MERGED = "crm.contact.merged"
    CRM_CONTACT_TAGGED = "crm.contact.tagged"
    CRM_CONTACT_NOTE_ADDED = "crm.contact.note_added"
    CRM_CONTACT_NOTE_UPDATED = "crm.contact.note_updated"
    CRM_CONTACT_NOTE_REMOVED = "crm.contact.note_removed"
    CRM_CONTACT_PERSON_ADDED = "crm.contact.person_added"
    CRM_CONTACT_PERSON_UPDATED = "crm.contact.person_updated"
    CRM_CONTACT_PERSON_REMOVED = "crm.contact.person_removed"
    CRM_CONTACT_ADDRESS_ADDED = "crm.contact.address_added"
    CRM_CONTACT_ADDRESS_UPDATED = "crm.contact.address_updated"
    CRM_CONTACT_ADDRESS_REMOVED = "crm.contact.address_removed"
    CRM_DEAL_CREATED = "crm.deal.created"
    CRM_DEAL_STAGE_CHANGED = "crm.deal.stage_changed"
    CRM_DEAL_WON = "crm.deal.won"
    CRM_DEAL_LOST = "crm.deal.lost"
    CRM_DEAL_UPDATED = "crm.deal.updated"
    CRM_DEAL_DELETED = "crm.deal.deleted"
    CRM_DEAL_REOPENED = "crm.deal.reopened"
    CRM_MEMO_CREATED = "crm.memo.created"
    CRM_MEMO_ITEM_ADDED = "crm.memo.item_added"
    CRM_MEMO_ITEM_REMOVED = "crm.memo.item_removed"
    CRM_MEMO_APPROVED = "crm.memo.approved"
    CRM_MEMO_CANCELLED = "crm.memo.cancelled"
    CRM_MEMO_INVOICED = "crm.memo.invoiced"
    CRM_MEMO_RETURNED = "crm.memo.returned"

    # Documents
    DOC_CREATED = "doc.created"
    DOC_UPDATED = "doc.updated"
    DOC_PATCHED = "doc.patched"
    DOC_LINKED = "doc.linked"
    DOC_FINALIZED = "doc.finalized"
    DOC_VOIDED = "doc.voided"
    DOC_SENT = "doc.sent"
    DOC_PAYMENT_RECEIVED = "doc.payment.received"
    DOC_PAYMENT_REFUNDED = "doc.payment.refunded"
    DOC_CONVERTED = "doc.converted"
    DOC_RECEIVED = "doc.received"
    DOC_ITEMS_RETURNED = "doc.items_returned"

    # Manufacturing
    MFG_ORDER_CREATED = "mfg.order.created"
    MFG_ORDER_STARTED = "mfg.order.started"
    MFG_ORDER_COMPLETED = "mfg.order.completed"
    MFG_ORDER_CANCELLED = "mfg.order.cancelled"
    MFG_STEP_COMPLETED = "mfg.step.completed"

    # Scanning
    SCAN_BARCODE = "scan.barcode"
    SCAN_RFID = "scan.rfid"
    SCAN_NFC = "scan.nfc"
    SCAN_RESOLVED = "scan.resolved"

    # Marketplace
    MP_LISTING_CREATED = "mp.listing.created"
    MP_LISTING_UPDATED = "mp.listing.updated"
    MP_LISTING_PUBLISHED = "mp.listing.published"
    MP_LISTING_UNPUBLISHED = "mp.listing.unpublished"
    MP_ORDER_RECEIVED = "mp.order.received"
    MP_ORDER_FULFILLED = "mp.order.fulfilled"
    MP_ORDER_CANCELLED = "mp.order.cancelled"

    # Accounting
    ACC_JOURNAL_ENTRY_CREATED = "acc.journal_entry.created"
    ACC_JOURNAL_ENTRY_POSTED = "acc.journal_entry.posted"
    ACC_JOURNAL_ENTRY_VOIDED = "acc.journal_entry.voided"
    ACC_PERIOD_CLOSED = "acc.period.closed"
    ACC_PERIOD_REOPENED = "acc.period.reopened"

    # Subscriptions (recurring orders)
    SUB_CREATED = "sub.created"
    SUB_UPDATED = "sub.updated"
    SUB_PAUSED = "sub.paused"
    SUB_RESUMED = "sub.resumed"
    SUB_GENERATED = "sub.generated"
    SUB_EXPIRED = "sub.expired"

    # System
    SYS_COMPANY_CREATED = "sys.company.created"
    SYS_USER_CREATED = "sys.user.created"
    SYS_USER_DEACTIVATED = "sys.user.deactivated"
    SYS_API_KEY_CREATED = "sys.api_key.created"
    SYS_API_KEY_REVOKED = "sys.api_key.revoked"
    SYS_BACKUP_CREATED = "sys.backup.created"
    SYS_MIGRATION_APPLIED = "sys.migration.applied"
