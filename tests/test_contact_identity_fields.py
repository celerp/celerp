# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A contact's email must be an address and its name may not be blanked, at every write; the
company's own contact (its identity on documents) is changed only by a role that may manage
company settings, and the screens say which record it is."""
from __future__ import annotations

from test_helpers import invite_user, register_admin, ui_request


async def _self_contact_id(client, h) -> str:
    return (await client.get("/companies/me", headers=h)).json()["settings"]["self_contact_id"]


async def _customer(client, h, **extra) -> str:
    r = await client.post("/crm/contacts", json={"name": "Acme Ltd", "contact_type": "customer", **extra}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _patch(client, h, cid, field, new):
    return await client.patch(f"/crm/contacts/{cid}", json={"fields_changed": {field: {"old": None, "new": new}}}, headers=h)


def _refused(r, key: str, status: int = 422) -> None:
    assert r.status_code == status, r.text
    assert r.json()["detail"]["message_key"] == key, r.text


async def test_email_must_be_an_address_on_every_write(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = await _customer(client, h, email="buyer@acme.example")
    _refused(await _patch(client, h, cid, "email", "not-an-email"), "error.email_invalid")
    _refused(await client.post("/crm/contacts", json={"name": "B", "email": "a b@x.example"}, headers=h), "error.email_invalid")
    _refused(await client.post(f"/crm/contacts/{cid}/people", json={"name": "Pat", "email": "pat@"}, headers=h),
             "error.email_invalid")
    assert (await client.get(f"/crm/contacts/{cid}", headers=h)).json()["email"] == "buyer@acme.example"
    assert (await _patch(client, h, cid, "email", "sales@acme.example")).status_code == 200
    assert (await _patch(client, h, cid, "email", "")).status_code == 200  # clearing it is allowed


async def test_name_cannot_be_blanked(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = await _customer(client, h)
    _refused(await _patch(client, h, cid, "name", "   "), "contacts.err_name_required")
    assert (await client.get(f"/crm/contacts/{cid}", headers=h)).json()["name"] == "Acme Ltd"


async def test_company_identity_needs_company_settings_permission(client, session):
    admin_h = {"Authorization": f"Bearer {await register_admin(client)}"}
    op_h = {"Authorization": f"Bearer {await invite_user(client, session, admin_h, 'op@perm.example', 'operator')}"}
    sid = await _self_contact_id(client, admin_h)
    cid = await _customer(client, admin_h)
    assert (await _patch(client, op_h, cid, "email", "a@acme.example")).status_code == 200
    _refused(await _patch(client, op_h, sid, "email", "a@acme.example"), "error.permission_missing", 403)
    _refused(await client.post(f"/crm/contacts/{sid}/addresses", json={"line1": "1 Road"}, headers=op_h),
             "error.permission_missing", 403)
    _refused(await client.post(f"/crm/contacts/{sid}/people", json={"name": "Pat", "is_primary": True}, headers=op_h),
             "error.permission_missing", 403)
    assert (await _patch(client, admin_h, sid, "email", "hello@perm.example")).status_code == 200


async def test_refused_cell_keeps_the_stored_value_and_says_why(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = await _customer(client, h)
    r = await ui_request(h, "PATCH", f"/contacts/{cid}/field/name", data={"value": ""})
    assert "Acme Ltd" in r.text and 'hx-trigger="click"' in r.text
    assert "Enter a name for the contact" in r.headers.get("hx-trigger", "")
    r = await ui_request(h, "PATCH", f"/contacts/{cid}/field/email", data={"value": "not-an-email"})
    assert "not-an-email" not in r.text and "Enter one email address" in r.headers.get("hx-trigger", "")


async def test_contact_fields_open_on_one_click(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = await _customer(client, h)
    page = (await ui_request(h, "GET", f"/contacts/{cid}")).text
    assert f'hx-get="/contacts/{cid}/field/email/edit"' in page
    assert 'hx-trigger="dblclick"' not in page


async def test_removing_a_tag_keeps_the_others(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    cid = await _customer(client, h)
    assert (await client.post(f"/crm/contacts/{cid}/tags", json={"tags": ["vip", "export"]}, headers=h)).status_code == 200
    r = await ui_request(h, "POST", f"/contacts/{cid}/tags/remove", data={"tag": "vip"})
    assert r.status_code == 200, r.text
    assert (await client.get(f"/crm/contacts/{cid}", headers=h)).json()["tags"] == ["export"]


async def test_company_own_contact_is_marked_and_opens_company_details(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _self_contact_id(client, h)
    listing = (await ui_request(h, "GET", "/contacts/customers")).text
    assert "Your company" in listing and 'href="/finance/company-details"' in listing
    r = await ui_request(h, "GET", f"/contacts/{sid}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/finance/company-details"
    details = (await ui_request(h, "GET", "/finance/company-details")).text
    assert "sender on your documents" in details


async def test_company_own_contact_is_never_deleted_or_merged_away(client):
    h = {"Authorization": f"Bearer {await register_admin(client)}"}
    sid = await _self_contact_id(client, h)
    cid = await _customer(client, h)
    _refused(await client.post("/crm/contacts/bulk/delete", json={"contact_ids": [sid]}, headers=h),
             "contacts.err_own_contact_kept")
    _refused(await client.post("/crm/contacts/merge", json={"target_contact_id": cid, "source_contact_ids": [sid]},
                               headers=h), "contacts.err_own_contact_kept")
    assert not (await client.get(f"/crm/contacts/{sid}", headers=h)).json().get("deleted")
