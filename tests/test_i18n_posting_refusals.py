# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Posting-account refusals, their status text and the upgrade notices, in the user's language.

The API says why a posting account cannot be used in English with a ``message_key`` and
its ``params``; the UI shows the key's translation with the role's label and the account
types in the user's language. A notice stores the keys its title and body were written
from, and the bell shows them in the reader's language.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
from fasthtml.common import to_xml
from sqlalchemy import select

from ui import i18n

ROOT = Path(__file__).resolve().parents[1]
LOCALES = sorted(p.stem for p in (ROOT / "ui" / "locales").glob("*.json"))
KEYS = ["posting.problem.unset", "posting.problem.not_in_chart", "posting.problem.inactive",
        "posting.problem.wrong_type", "posting.problem.header", "posting.refused",
        "posting.continued_account_unusable", "posting.type_or",
        "notice.posting_unmapped.title", "notice.posting_unmapped.body",
        "notice.older_stock_unplaced.title", "notice.older_stock_unplaced.body",
        "notice.older_stock_moved.title", "notice.older_stock_moved.body",
        "notice.mfg_reconcile_needed.title", "notice.mfg_reconcile_needed.body",
        "notice.mfg_wip_recorded.title", "notice.mfg_wip_recorded.body",
        "notice.historical_lots_no_product.title", "notice.historical_lots_no_product.body",
        "notice.starter_modules_on.title", "notice.starter_modules_on.body",
        "notice.work_centers_moved.title", "notice.work_centers_moved.body",
        "notice.imported_doc_cutover.title", "notice.imported_doc_cutover.body"]
_ASSET = {"code": "1110", "account_type": "asset", "is_active": True, "has_children": False}


@pytest.fixture(autouse=True)
def _lang():
    yield
    i18n.set_lang("en")


def _catalog(lang: str) -> dict:
    return json.loads((ROOT / "ui" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


def shown_in(lang: str, notice) -> dict:
    """The notice as the bell lists it to a reader of ``lang``."""
    from ui.routes.notifications import _in_reader_language

    listed = {"items": [{"id": str(notice.id), "title": notice.title, "body": notice.body, "i18n": notice.i18n}]}
    i18n.set_lang(lang)
    try:
        [item] = json.loads(_in_reader_language(json.dumps(listed).encode()))["items"]
    finally:
        i18n.set_lang("en")
    return item


@pytest.mark.parametrize("lang", LOCALES)
def test_every_language_carries_each_posting_refusal_and_notice_with_the_same_fields(lang):
    en, catalog = _catalog("en"), _catalog(lang)
    for key in KEYS:
        assert key in catalog, f"{lang} lacks {key}"
        assert set(re.findall(r"\{(\w+)", catalog[key])) == set(re.findall(r"\{(\w+)", en[key])), (lang, key)


def test_a_wrong_type_account_is_explained_in_the_users_language():
    from celerp.accounting_roles import target_problem

    problem = target_problem("sales_revenue", {"sales_revenue": "1110"}, _ASSET)
    i18n.set_lang("de")
    de = _catalog("de")
    assert i18n.refusal_text(problem) == (
        f"{de['posting.role.sales_revenue']} ist auf Konto 1110 vom Typ {de['enum.account_type.asset']} gesetzt; "
        f"es muss vom Typ {de['enum.account_type.revenue']} sein.")
    assert "a asset" not in problem["message"]


def test_an_api_refusal_reaches_every_ui_site_in_the_users_language():
    """ui.api_client turns a structured refusal into the user's language once, so a site
    that shows ``APIError.detail`` needs nothing of its own."""
    from celerp.services.account_roles import PostingRoleError, target_problems
    from ui.api_client import APIError, _raise

    error = PostingRoleError(list(target_problems(["cogs"], {}, None).values()))
    assert error.detail["message"] == ("Cost of goods sold has no account set. "
                                       "Choose the account in Settings > Accounting > Posting accounts.")
    response = httpx.Response(409, json={"detail": error.detail}, request=httpx.Request("PUT", "http://x/"))
    i18n.set_lang("de")
    with pytest.raises(APIError) as exc:
        _raise(response)
    label = _catalog("de")["posting.role.cogs"]
    assert exc.value.detail == (f"Für {label} ist kein Konto festgelegt. "
                                "Wählen Sie das Konto unter Einstellungen > Buchhaltung > Buchungskonten.")
    assert exc.value.data == error.detail


def test_the_posting_accounts_status_is_shown_in_the_users_language():
    from celerp.accounting_roles import target_problem
    from ui.routes.settings_accounting import _posting_role_row

    row = {"role": "sales_revenue", "label": "Sales revenue", "code": "1110", "name": "Cash", "status": "wrong_type",
           "problem": target_problem("sales_revenue", {"sales_revenue": "1110"}, _ASSET), "earlier": []}
    i18n.set_lang("de")
    html = to_xml(_posting_role_row(row))
    assert "ist auf Konto 1110" in html and "is set to account" not in html


@pytest.mark.asyncio
async def test_an_unmapped_posting_account_notice_is_shown_in_the_readers_language(session, auth):
    from celerp.accounting_roles import ROLES_KEY
    from celerp.models.notification import Notification
    from celerp.services.company_lock import locked_company
    from celerp.services.posting_readiness import notify_unmapped
    from ui.routes.notifications import _in_reader_language

    cid = auth["company_id"]
    company = await locked_company(session, cid)
    roles = dict(company.settings[ROLES_KEY])
    roles.pop("general_expense")
    company.settings = {**company.settings, ROLES_KEY: roles}
    await session.flush()
    assert await notify_unmapped(session, cid)
    notice = (await session.execute(select(Notification).where(Notification.company_id == cid))).scalar_one()
    assert notice.i18n == {"title": "notice.posting_unmapped.title", "body": "notice.posting_unmapped.body",
                           "params": {"roles": ["general_expense"]}}

    listed = {"items": [{"id": str(notice.id), "title": notice.title, "body": notice.body, "i18n": notice.i18n},
                        {"id": "x", "title": "Stored as written", "body": "Kept", "i18n": None}],
              "unread_count": 2}
    i18n.set_lang("de")
    shown = json.loads(_in_reader_language(json.dumps(listed).encode()))
    de = _catalog("de")
    assert shown["items"][0]["title"] == de["notice.posting_unmapped.title"]
    assert shown["items"][0]["body"] == de["notice.posting_unmapped.body"].format(
        roles=de["posting.role.general_expense"])
    assert shown["items"][1] == {"id": "x", "title": "Stored as written", "body": "Kept"}


@pytest.mark.asyncio
async def test_the_work_center_notice_an_upgrade_stored_is_shown_in_the_readers_language(client, session, auth):
    """The upgrade that moved Hours per day onto work centers stored its notice as English text
    with no keys, the way that release wrote it. The bell still shows it in the reader's language."""
    from sqlalchemy import text

    from celerp.migrations.versions.f8a9b0c1d2e3_work_center_default_backfill import _NOTICE_BODY, _NOTICE_TITLE
    from ui.routes.notifications import _in_reader_language

    await session.execute(text("""
        INSERT INTO notifications (id, company_id, user_id, category, title, body, action_url, priority, created_at)
        VALUES (gen_random_uuid(), :cid, NULL, 'manufacturing', :title, :body, '/settings/manufacturing', 'high', NOW())
    """), {"cid": auth["company_id"], "title": _NOTICE_TITLE, "body": _NOTICE_BODY})
    await session.commit()

    listed = await client.get("/notifications", headers=auth["headers"])
    assert listed.status_code == 200, listed.text
    i18n.set_lang("de")
    [shown] = json.loads(_in_reader_language(listed.content))["items"]
    de = _catalog("de")
    assert (shown["title"], shown["body"]) == (de["notice.work_centers_moved.title"],
                                               de["notice.work_centers_moved.body"])
    assert shown["action_url"] == "/settings/manufacturing"
