# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Exceptional repricing feedback must not alter the normal document layout."""
from __future__ import annotations

from pathlib import Path


def test_reprice_is_gated_on_successful_save_and_pins_entity_version():
    source = Path("ui/routes/documents.py").read_text()
    start = source.index("async function celerpReprice")
    snippet = source[start:start + 2200]
    assert "clearTimeout(_celerpSaveTimer);" in snippet
    assert "return _celerpMutate(async () =>" in snippet
    assert "const ok = await _celerpPersistOnce();" in snippet
    assert "if (!ok) {" in snippet
    assert "_celerpRestorePriceList();" in snippet
    assert "expected_version: _celerpEntityVersion" in snippet


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


def test_list_writes_share_one_version_and_mutation_coordinator():
    source = Path("ui/routes/documents.py").read_text()
    assert '"HX-Trigger": _json.dumps({' in source
    assert '"celerpListVersion": {"version": result.get("version")}' in source
    assert "document.body.addEventListener('celerpListVersion'" in source
    assert "window._celerpEntityVersion" in source
    assert "window._celerpMutationTail" in source
    assert "function _celerpMutate(run)" in source
    assert "function _celerpPatchListField(" in source
    assert "return _celerpMutate(_celerpPersistOnce);" in source
    assert "return _celerpMutate(async () =>" in source
    assert 'hx_patch=f"/lists/{entity_id}/field/' not in source


def test_cost_reprice_permission_precedes_replay_lookup():
    source = Path("default_modules/celerp-docs/celerp_docs/routes.py").read_text()
    for name in ("reprice_doc", "reprice_list"):
        start = source.index(f"async def {name}")
        snippet = source[start:start + 1800]
        assert snippet.index("_assert_reprice_access(") < snippet.index(
            "find_event_by_idempotency(session, company_id, idem_key)"
        )


def test_repricing_business_logic_exists_only_in_backend_primitive():
    ui_source = Path("ui/routes/documents.py").read_text()
    backend = Path("default_modules/celerp-docs/celerp_docs/routes.py").read_text()

    doc_proxy = ui_source[ui_source.index("async def reprice_doc_lines"):
                          ui_source.index("# T3: Document actions")]
    assert "api.reprice_doc" in doc_proxy
    assert "resolve_price(" not in doc_proxy
    assert "api.get_item(" not in doc_proxy
    assert "api.list_items(" not in doc_proxy

    field_start = ui_source.index("# Price-list changes are one domain operation")
    field_snippet = ui_source[field_start:field_start + 1800]
    assert "api.reprice_doc" in field_snippet
    assert "resolve_price(" not in field_snippet
    assert "api.get_item(" not in field_snippet
    assert "api.list_items(" not in field_snippet

    assert backend.count("await _reprice_catalog_lines(") == 2


def test_reprice_warning_reapplies_after_paged_htmx_swap():
    """Skipped rows can live off the first page, so paging must reapply the
    persisted warning state after HTMX replaces the line section."""
    source = Path("ui/routes/documents.py").read_text()
    assert "window._celerpRepriceWarningAfterSwap = _celerpApplyRepriceWarnings" in source
    assert "document.body.addEventListener('htmx:afterSwap', window._celerpRepriceWarningAfterSwap)" in source


def test_doc_reprice_reuses_canonical_price_override_permission_gate():
    source = Path("default_modules/celerp-docs/celerp_docs/routes.py").read_text()
    start = source.index("async def reprice_doc(")
    end = source.index("@lists_router.post", start)
    snippet = source[start:end]
    assert "await _assert_sales_line_price_permission(" in snippet


def test_sales_price_permission_is_shared_by_doc_and_quotation_write_boundaries():
    source = Path("default_modules/celerp-docs/celerp_docs/routes.py").read_text()
    assert source.count("await _assert_sales_line_price_permission(") >= 7
    for name in ("create_list", "patch_list", "reprice_list", "patch_list_line_page"):
        start = source.index(f"async def {name}(")
        snippet = source[start:start + 9000]
        assert "_assert_sales_line_price_permission(" in snippet



def test_inline_doc_writes_advance_cached_reprice_version():
    source = Path("ui/routes/documents.py").read_text()

    field_start = source.index('async def doc_field_patch(')
    field_end = source.index('# Line-item fields editable on finalized docs', field_start)
    field_snippet = source[field_start:field_end]
    assert '"celerpListVersion": {"version": doc.get("version")}' in field_snippet

    autosave_start = source.index('async def doc_field_post(')
    autosave_end = source.index('@app.post("/docs/{entity_id}/notes")', autosave_start)
    autosave_snippet = source[autosave_start:autosave_end]
    assert 'version = result.get("event_id")' in autosave_snippet
    assert '"celerpListVersion": {"version": version}' in autosave_snippet


def test_reprice_failure_never_leaves_selector_in_uncommitted_state():
    source = Path("ui/routes/documents.py").read_text()
    start = source.index("window._CELERP_AUTHORITATIVE_PRICE_LIST")
    end = source.index("/* ── CSV import ── */", start)
    snippet = source[start:end]
    assert "function _celerpRestorePriceList()" in snippet
    assert "if (!ok) {" in snippet
    assert "_celerpRestorePriceList();" in snippet
    assert "try {" in snippet and "catch (err)" in snippet
    assert snippet.count("window.location.reload();") >= 2
