# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for migration l9a0b1c2d3e4: Lists stored with customer_id, customer_name or
receiver carry their counterparty as contact_id and contact_name."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text

_MOD = "l9a0b1c2d3e4_fold_list_contact_fields"


def _insert(mig_db, entity_type: str, state: dict) -> tuple[str, str]:
    cid, eid = str(uuid.uuid4()), f"{entity_type}:{uuid.uuid4()}"
    with mig_db.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO projections (entity_id, company_id, entity_type, state)"
            " VALUES (:eid, :cid, :t, CAST(:state AS jsonb))"
        ), {"eid": eid, "cid": cid, "t": entity_type, "state": json.dumps(state)})
    return cid, eid


def test_a_list_with_customer_fields_shows_them_as_its_contact(mig_db):
    cid, eid = _insert(mig_db, "list", {
        "ref_id": "Q-1", "customer_id": "contact:A", "customer_name": "Harbour Buyer"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid) == {
        "ref_id": "Q-1", "contact_id": "contact:A", "contact_name": "Harbour Buyer"}


def test_a_transfer_receiver_becomes_the_contact_name(mig_db):
    cid, eid = _insert(mig_db, "list", {"ref_id": "T-1", "receiver": "Branch Two"})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid) == {"ref_id": "T-1", "contact_name": "Branch Two"}


def test_a_contact_already_on_the_list_is_kept(mig_db):
    cid, eid = _insert(mig_db, "list", {
        "contact_id": "contact:B", "contact_name": "Current", "customer_id": "contact:A",
        "customer_name": "Older"})

    mig_db.run_upgrade(_MOD)
    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid) == {"contact_id": "contact:B", "contact_name": "Current"}


def test_other_records_are_untouched(mig_db):
    cid, eid = _insert(mig_db, "doc", {"customer_name": "Kept", "contact_name": ""})

    mig_db.run_upgrade(_MOD)

    assert mig_db.get_state(cid, eid) == {"customer_name": "Kept", "contact_name": ""}
