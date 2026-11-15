# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A role without a view permission reads none of that record kind, by any route.

Each record kind's list and detail need its view permission; its notes, files, PDF,
share state, export, previews and reports read the same records and need the same one.
Numbering is read on the screen that changes it, so it needs that screen's permission.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from test_helpers import grant_permission, perm_setup

_SMALL_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


async def _ok(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


async def _denied_then_allowed(client, s, permission: str, paths: list[str]) -> None:
    """Every path refuses the operator while only managers hold ``permission``, and
    answers once the operator holds it again."""
    owner, operator = s["admin_h"], s["operator_h"]
    await grant_permission(client, owner, permission, "manager")
    got = {p: (await client.get(p, headers=operator)).status_code for p in paths}
    assert got == {p: 403 for p in paths}
    await grant_permission(client, owner, permission, "operator")
    got = {p: (await client.get(p, headers=operator)).status_code for p in paths}
    assert got == {p: 200 for p in paths}


@pytest.mark.asyncio
async def test_contact_notes_files_and_export_need_view_contacts(client, session):
    s = await perm_setup(client, session)
    owner = s["admin_h"]
    cid = (await _ok(await client.post("/crm/contacts", json={"name": "Alice"}, headers=owner)))["id"]
    await _ok(await client.post(f"/crm/contacts/{cid}/notes", json={"note": "Prefers mornings"}, headers=owner))
    fid = (await _ok(await client.post(
        f"/crm/contacts/{cid}/files", files={"file": ("a.pdf", _SMALL_PDF, "application/pdf")}, headers=owner,
    )))["id"]
    # The export is the list itself, so it needs exporting as the item and document exports do.
    await grant_permission(client, owner, "import_export_data", "operator")
    await _denied_then_allowed(client, s, "view_contacts", [
        f"/crm/contacts/{cid}/notes", f"/crm/contacts/{cid}/files/{fid}", "/crm/contacts/export/csv",
    ])


@pytest.mark.asyncio
async def test_contact_export_needs_import_export_data(client, session):
    s = await perm_setup(client, session)
    await grant_permission(client, s["admin_h"], "import_export_data", "manager")
    assert (await client.get("/crm/contacts/export/csv", headers=s["operator_h"])).status_code == 403
    assert (await client.get("/crm/contacts/export/csv", headers=s["manager_h"])).status_code == 200


@pytest.mark.asyncio
async def test_document_pdf_notes_and_share_state_need_view_documents(client, session):
    s = await perm_setup(client, session)
    owner = s["admin_h"]
    doc = (await _ok(await client.post("/docs", json={"doc_type": "invoice"}, headers=owner)))["id"]
    await _ok(await client.post(f"/docs/{doc}/notes", json={"note": "Call first"}, headers=owner))
    await _denied_then_allowed(client, s, "view_documents", [
        f"/docs/{doc}/pdf", f"/docs/{doc}/notes", f"/docs/{doc}/share/state",
    ])


@pytest.mark.asyncio
async def test_document_numbering_needs_the_numbering_screen_permission(client, session):
    s = await perm_setup(client, session)
    await _denied_then_allowed(client, s, "manage_module_settings", ["/docs/sequences"])


@pytest.mark.asyncio
async def test_item_values_split_preview_and_expiring_need_view_inventory(client, session):
    s = await perm_setup(client, session)
    owner = s["admin_h"]
    soon = (date.today() + timedelta(days=5)).isoformat()
    await _ok(await client.post("/items", json={
        "sku": "SKU-EXP", "name": "Milk", "quantity": 3, "location_id": s["location_id"],
        "sell_by": "piece", "status": "available", "expires_at": soon,
    }, headers=owner))
    await _denied_then_allowed(client, s, "view_inventory", [
        "/items/field-values?field=status", f"/items/{s['item_id']}/split-preview", "/reports/expiring?days=30",
    ])


@pytest.mark.asyncio
async def test_item_field_values_keep_costs_from_a_role_without_costs(client, session):
    s = await perm_setup(client, session)
    owner, operator = s["admin_h"], s["operator_h"]
    assert (await _ok(await client.get("/items/field-values?field=cost_price", headers=owner)))["values"] == ["100.0"]
    await grant_permission(client, owner, "view_inventory_costs", "manager")
    assert (await _ok(await client.get("/items/field-values?field=cost_price", headers=operator)))["values"] == []


def _ungated_reads_beside_view_siblings(app) -> list[str]:
    """GET routes no permission (one, or any of several), agent gate or body refusal guards
    whose first path segment holds a GET that needs a view permission, unless the any-role
    marker says why."""
    from collections import defaultdict

    from celerp.ai import tools as ai_tools

    routes = ai_tools._api_routes(app)
    segment = lambda path: path.strip("/").split("/")[0]  # noqa: E731
    views: dict[str, set[str]] = defaultdict(set)
    for path, methods, route in routes:
        if "GET" in methods:
            views[segment(path)].update(k for k in ai_tools._route_permissions(route) if k.startswith("view_"))
    problems = []
    for path, methods, route in routes:
        if "GET" not in methods or not views[segment(path)]:
            continue
        if ai_tools._route_permissions(route) or ai_tools.route_gates(route) or ai_tools.refusing_checks(route) \
                or (route.openapi_extra or {}).get("x-celerp-agent-any-permission"):
            continue  # gated by a permission, a gate, a body refusal, or any one of several permissions
        why = (route.openapi_extra or {}).get("x-celerp-agent-any-role")
        if not (isinstance(why, str) and why.strip()):
            problems.append(f"GET {path}: any role reads it beside {sorted(views[segment(path)])}")
    return sorted(problems)


def test_no_read_answers_every_role_beside_view_gated_siblings():
    """A read beside reads that need a view permission needs one too, or its any-role
    marker names why every role may read it (its own data, a shared setting)."""
    from test_helpers import real_agent_app

    assert _ungated_reads_beside_view_siblings(real_agent_app()) == []
