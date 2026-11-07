# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Only an agreement's schedule writes the fields it owns, and no writer takes a field an
earlier release kept on a template. Both sides are read from the code (owned_fields_census):
a key billing or migration writes on an agreement that the Documents module does not own
fails here, and so does a new writer of documents that runs no check. Every owned and removed
key is then refused by name at each door a caller's data comes in by: create, edit and import."""
from __future__ import annotations

import uuid

import pytest

from celerp_docs.doc_constants import (
    LIFECYCLE_OWNED_FIELDS, SCHEDULE_OWNED_FIELDS, SCHEDULE_REMOVED_FIELDS, SCHEDULE_TERM_FIELDS,
)

from agreement_kit import LINE, agreement, freeze_day, register
from owned_fields_census import doors, writes, written_keys


# Keys the schedule writes that a caller may send too, and why.
CALLER_KEYS = {
    "start_date": "a draft names its own start date; activate fills it only when the draft left it empty",
    "status": "a new record may say it is a draft (_created_as_draft); any other status is lifecycle-owned",
}
# Writers of documents that run no owned-field check, and why their data never holds a caller's keys.
SERVER_BUILT = {
    "celerp/events/engine.py::connector_upsert": "the primitive; each connector's caller is its own row here",
    "celerp/services/attachments.py::attach_file": "writes only the files list it builds",
    "default_modules/celerp-admin/celerp_admin/routes.py::_check_inverted_doc_dates":
        "rewrites the issue and due dates of a document already stored",
    "default_modules/celerp-contacts/celerp_contacts/routes.py::merge_contacts_service":
        "rewrites only the contact a document names",
    "default_modules/celerp-docs/celerp_docs/doc_service.py::_emit_doc":
        "builds a WooCommerce order from the fields it maps, never the store's raw data",
    "default_modules/celerp-docs/celerp_docs/doc_service.py::_record_woocommerce_hold": "writes the hold it computes",
    "default_modules/celerp-docs/celerp_docs/doc_service.py::set_woocommerce_order_reconciled":
        "writes the reconciled mark",
    "default_modules/celerp-docs/celerp_docs/doc_service.py::upsert_order_from_woocommerce":
        "builds the order from the fields it maps",
    "default_modules/celerp-docs/celerp_docs/imported_cutover.py::_correct": "corrects the cutover figures it computes",
    "default_modules/celerp-docs/celerp_docs/legacy_credit_notes.py::_void_with_invoice":
        "moves stored credit notes onto the current shape",
    "default_modules/celerp-docs/celerp_docs/legacy_credit_notes.py::_post": "moves stored credit notes onto the current shape",
    "default_modules/celerp-docs/celerp_docs/routes.py::emit_credit_settlement": "writes the settlement it computes",
    "default_modules/celerp-docs/celerp_docs/routes.py::set_memo_tax_rate": "writes the tax rate it is given",
    "default_modules/celerp-docs/celerp_docs/routes.py::record_historical_delivery": "writes the delivery it computes",
    "default_modules/celerp-docs/celerp_docs/routes.py::_supplier_credit_for_return":
        "builds the supplier credit from the return",
    "default_modules/celerp-docs/celerp_docs/routes.py::reprice_doc": "writes the line prices it computes",
    "default_modules/celerp-docs/celerp_docs/routes.py::convert_list": "builds the document from the list's lines",
    "default_modules/celerp-docs/celerp_docs/routes.py::_apply_split_plan": "writes only line items",
    "default_modules/celerp-docs/celerp_docs/routes.py::_give_lines_ids": "writes only line items",
    "default_modules/celerp-fixed-assets/celerp_fixed_assets/demo.py::seed_demo_assets": "seeds the demo documents it builds",
    "default_modules/celerp-fixed-assets/celerp_fixed_assets/demo.py::_follow_books_currency": "writes the currency",
    "default_modules/celerp-fixed-assets/celerp_fixed_assets/sales.py::_draft": "builds the sale draft from the asset",
    "default_modules/celerp-fixed-assets/celerp_fixed_assets/store.py::_emit": "the asset store's own events",
    "default_modules/celerp-manufacturing/celerp_manufacturing/movements.py::emit": "the manufacturing movements it builds",
    "default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::write_agreement": "the schedule, the owner",
    "default_modules/celerp-subscriptions/celerp_subscriptions/billing.py::_bill": "the schedule's own bill, the owner",
}
# Every key a caller's data is refused for: what the schedule writes, the owned set and the removed fields.
REFUSED = sorted((written_keys() - SCHEDULE_TERM_FIELDS - set(CALLER_KEYS)) | SCHEDULE_OWNED_FIELDS | SCHEDULE_REMOVED_FIELDS)


def test_the_census_reads_every_agreement_write():
    unread = [(w.where, w.unresolved) for w in writes() if w.unresolved]
    assert not unread, f"the census cannot read these writes; teach it the new shape: {unread}"


def test_every_key_the_schedule_writes_is_owned():
    written = written_keys()
    unowned = written - LIFECYCLE_OWNED_FIELDS - SCHEDULE_TERM_FIELDS - SCHEDULE_REMOVED_FIELDS - set(CALLER_KEYS)
    assert not unowned, f"billing or migration writes {sorted(unowned)}: add them to SCHEDULE_OWNED_FIELDS"
    assert set(CALLER_KEYS) <= written, f"stale caller keys: {sorted(set(CALLER_KEYS) - written)}"


def test_every_writer_of_documents_checks_owned_fields_or_says_why_it_need_not():
    unchecked = {d.function for d in doors() if d.check is None}
    assert not unchecked - set(SERVER_BUILT), (
        f"these write documents and run no owned-field check: {sorted(unchecked - set(SERVER_BUILT))}")
    assert not set(SERVER_BUILT) - unchecked, f"stale entries: {sorted(set(SERVER_BUILT) - unchecked)}"


def _refused(r, key: str) -> bool:
    return r.status_code == 422 and key in r.text


@pytest.mark.parametrize("key", REFUSED)
async def test_create_edit_and_import_refuse_an_owned_or_removed_field_by_name(client, key):
    hd = await register(client)
    sent = {"doc_type": "subscription_invoice", "contact_id": "contact:test-001", "frequency": "monthly",
            "start_date": "2026-03-01", "line_items": [dict(LINE)], key: {"doc:INV-0001": 0}}
    before = (await client.get("/docs", headers=hd)).json()
    r = await client.post("/docs", json=sent, headers=hd)
    assert _refused(r, key), (key, r.status_code, r.text)
    assert (await client.get("/docs", headers=hd)).json() == before

    draft = await agreement(client, hd, start="2026-03-01", activate=False)
    r = await client.patch(f"/docs/{draft}", json={"fields_changed": {key: {"new": {"doc:INV-0001": 0}}}}, headers=hd)
    assert _refused(r, key), (key, r.status_code, r.text)

    record = {"entity_id": f"doc:{uuid.uuid4()}", "event_type": "doc.created", "source": "test",
              "idempotency_key": uuid.uuid4().hex, "data": {**sent, "status": "draft"}}
    r = await client.post("/docs/import", json=record, headers=hd)
    assert _refused(r, key), (key, r.status_code, r.text)
    r = await client.post("/docs/import/batch", json={"records": [{**record, "idempotency_key": uuid.uuid4().hex}]},
                          headers=hd)
    assert r.status_code == 200 and r.json()["created"] == 0 and key in " ".join(r.json()["errors"]), r.text


@pytest.mark.parametrize("key", sorted(SCHEDULE_REMOVED_FIELDS))
async def test_an_import_update_refuses_a_removed_field(client, key):
    hd = await register(client)
    record = {"entity_id": f"doc:{uuid.uuid4()}", "event_type": "doc.created", "source": "test",
              "idempotency_key": uuid.uuid4().hex,
              "data": {"doc_type": "subscription_invoice", "contact_id": "contact:test-001", "status": "draft",
                       "frequency": "monthly", "start_date": "2026-03-01", "line_items": [dict(LINE)]}}
    assert (await client.post("/docs/import", json=record, headers=hd)).status_code == 200
    again = {**record, "data": {**record["data"], key: "2027-03-01"}}
    r = await client.post("/docs/import/batch", json={"upsert": True, "records": [again]}, headers=hd)
    assert r.status_code == 200 and r.json()["updated"] == 0 and key in " ".join(r.json()["errors"]), r.text


async def test_an_agreement_cannot_name_an_unrelated_invoice_as_a_bill_it_let_go(client, monkeypatch):
    """A plain create once forged released_bills, and the named invoice could never be issued."""
    freeze_day(monkeypatch, "2026-03-15")
    hd = await register(client)
    r = await client.post("/docs", headers=hd, json={
        "doc_type": "invoice", "contact_id": "contact:test-001", "line_items": [dict(LINE)]})
    assert r.status_code in (200, 201), r.text
    invoice = r.json()["id"]
    r = await client.post("/docs", headers=hd, json={
        "doc_type": "subscription_invoice", "contact_id": "contact:test-001", "frequency": "monthly",
        "start_date": "2026-03-01", "line_items": [dict(LINE)], "released_bills": {invoice: 0}})
    assert _refused(r, "released_bills"), r.text
    assert (await client.post(f"/docs/{invoice}/finalize", headers=hd, json={})).status_code == 200


async def test_a_new_agreement_cannot_carry_an_earlier_release_next_run_date(client):
    """An integration written for 2.8.0 is told the field is gone, instead of making a draft
    that anchors on it or cannot activate."""
    from ui.i18n import t

    hd = await register(client)
    r = await client.post("/docs", headers=hd, json={
        "doc_type": "subscription_invoice", "contact_id": "contact:test-001", "frequency": "monthly",
        "start_date": "2026-11-01", "next_run_date": "2027-03-01", "line_items": [dict(LINE)]})
    assert r.status_code == 422, r.text
    assert t("documents.err_schedule_fields_removed", fields="next_run_date") in r.text
