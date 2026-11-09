# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Browser test: a record the company refuses to show is never drawn as an empty one.

A team member whose role may not view documents opens a List or a document by its link
(a bookmark, a shared URL, a notification). The page they land on says they do not have
access, the same notice the documents page gives, instead of an empty Draft record.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from ui.i18n import t

from .conftest import set_session_cookies, wait_for_topbar
from .test_company_backup_browser import _add_user, _token_for
from .test_gl_impact_screens import _demo_company, _stocked_item

pytestmark = pytest.mark.browser


def _finalized_quotation_list(api) -> dict:
    _demo_company(api)
    item = _stocked_item(api)
    r = api.post("/lists", json={"list_type": "quotation", "line_items": [
        {"item_id": item["id"], "sku": item.get("sku"), "name": item.get("name"),
         "quantity": 1, "unit_price": 10.0, "line_total": 10.0}]})
    assert r.status_code == 200, r.text
    list_id = r.json()["id"]
    assert api.post(f"/lists/{list_id}/finalize").status_code == 200
    return api.get(f"/lists/{list_id}").json()


def _viewer_without_documents(api) -> str:
    """A viewer whose role the owner has taken View documents from, signed in."""
    r = api.patch("/companies/me/role-permissions",
                  json={"perm_key": "view_documents", "role_key": "viewer", "granted": False})
    assert r.status_code == 200, r.text
    user_id, _email = _add_user(api, "viewer")
    return _token_for(user_id, api.get("/companies/me").json()["id"])


def test_a_list_opened_by_its_link_without_view_documents_says_so(page: Page, ui_server, fresh_company):
    api = fresh_company
    lst = _finalized_quotation_list(api)
    set_session_cookies(page.context, _viewer_without_documents(api))

    for url in (f"/lists/{lst['id']}", "/lists"):
        page.goto(f"{ui_server}{url}")
        wait_for_topbar(page)
        assert page.url.endswith("/dashboard"), (url, page.url)
        expect(page.get_by_text(t("perm.redirected_no_access"))).to_be_visible()
        assert "Draft" not in page.locator("#main-content").inner_text(), url

    # The print view has no app frame: it answers with the refusal itself, never the List.
    response = page.goto(f"{ui_server}/lists/{lst['id']}/print")
    assert response.status == 403
    assert lst["ref_id"] not in page.content()
