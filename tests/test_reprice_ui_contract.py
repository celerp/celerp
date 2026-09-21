# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Exceptional repricing feedback must not alter the normal document layout."""
from __future__ import annotations

from pathlib import Path


def test_reprice_is_gated_on_successful_save_and_pins_list_version():
    source = Path("ui/routes/documents.py").read_text()
    start = source.index("async function celerpReprice")
    snippet = source[start:start + 2200]
    assert "clearTimeout(_celerpSaveTimer);" in snippet
    assert "const ok = await _celerpPersist();" in snippet
    assert "if (!ok) return;" in snippet
    assert "body.expected_version = _celerpListVersion" in snippet


def test_partial_reprice_feedback_is_ephemeral_and_row_only():
    source = Path("ui/routes/documents.py").read_text()
    assert "sessionStorage.setItem(_celerpRepriceWarningKey()" in source
    assert "sessionStorage.removeItem(_celerpRepriceWarningKey())" in source
    assert "row.classList.add('doc-line--reprice-warning')" in source
    assert "dlg.showModal()" in source
    # The exceptional modal is transient; no line-table column or persistent control is introduced.
    css = Path("ui/static/app.css").read_text()
    rule = ".doc-lines tbody tr.doc-line--reprice-warning { outline: 2px solid var(--c-amber); outline-offset: -2px; }"
    assert rule in css
    assert "border:" not in rule


def test_company_help_reuses_existing_info_tip_only_when_requested():
    from fasthtml.common import to_xml
    from ui.routes.contacts import _contact_info_card

    contact = {"entity_id": "contact:self", "name": "Owner", "company_name": "Trading Co"}
    ordinary = to_xml(_contact_info_card(contact))
    company = to_xml(_contact_info_card(contact, company_name_help="Business name help"))

    assert "Business name help" not in ordinary
    assert 'class="info-tip"' not in ordinary
    assert "Business name help" in company
    assert company.count('class="info-tip"') == 1


def test_list_writes_share_one_version_and_serialized_save_path():
    source = Path("ui/routes/documents.py").read_text()
    assert '"HX-Trigger": _json.dumps({' in source
    assert '"celerpListVersion": {"version": result.get("version")}' in source
    assert "document.body.addEventListener('celerpListVersion'" in source
    assert "async function _celerpPersistOnce()" in source
    assert "window._celerpPersistTail" in source
    assert "window._celerpPersistTail.then(" in source


def test_cost_reprice_permission_precedes_replay_lookup():
    source = Path("default_modules/celerp-docs/celerp_docs/routes.py").read_text()
    start = source.index("async def reprice_list")
    snippet = source[start:start + 2200]
    assert snippet.index("if is_cost_list_name(payload.price_list):") < snippet.index(
        "find_event_by_idempotency(session, company_id, idem_key)"
    )
