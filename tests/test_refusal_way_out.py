# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A refusal's way out fits its reader: every refusal one page resolves, read by every
default role, links to that page only for a role that may take the step there, and
otherwise names the roles that can, never a link to a page the reader cannot use."""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest
from fasthtml.common import to_xml

from celerp.accounting_roles import REFUSAL_FIXES, REQUEST_COMPANY_SETTINGS, refusal
from celerp.services.auth import ROLE_LEVELS
from celerp.services.permissions import role_has_permission
from ui.api_client import APIError
from ui.components.shell import error_flash, refusal_fix, toast_header
from ui.i18n import _reader_role, refusal_text, t


@contextmanager
def _reader(role: str | None, settings: dict | None = None):
    token, company = _reader_role.set(role), REQUEST_COMPANY_SETTINGS.set(settings)
    try:
        yield
    finally:
        REQUEST_COMPANY_SETTINGS.reset(company)
        _reader_role.reset(token)


_GRANTS = [None, {"role_grants": {"manage_accounting": ["owner"], "manage_company_settings": ["owner"]}},
           {"role_grants": {"manage_accounting": list(ROLE_LEVELS)}}]


@pytest.mark.parametrize("settings", _GRANTS)
@pytest.mark.parametrize("key", sorted(REFUSAL_FIXES))
def test_every_fixable_refusal_reads_for_every_role_with_a_way_out_it_can_take(key, settings):
    href, label_key, permission = REFUSAL_FIXES[key]
    with _reader(None, settings):
        detail = refusal(key, "", bank_id="b1")
    able = [r for r in ROLE_LEVELS if role_has_permission(settings, r, permission)]
    assert sorted(detail["fix"]["roles"]) == sorted(able)
    for role in ROLE_LEVELS:
        with _reader(role):
            link, text = refusal_fix(detail), refusal_text(detail)
            flash = to_xml(error_flash(APIError(409, text, detail)))
            toast = json.loads(toast_header(detail, "error")["HX-Trigger"])["celerpToast"]
            if role in able:
                assert link == {"href": href.format(bank_id="b1"), "label": t(label_key)}
                assert f'href="{link["href"]}"' in flash and toast["action"] == link
                assert t("error.fix_ask_roles", place=t(label_key), roles="")[:20] not in text
            else:
                assert link is None and "href" not in flash and "action" not in toast
                assert t(label_key) in text and all(t(f"settings.{r}") in text for r in able), (role, text)


async def test_a_manager_asking_the_client_is_told_who_can_add_one(client, session):
    """The bank's Ask the client with no client member, asked by a manager: the refusal
    names the roles that manage company settings instead of linking to the users tab."""
    import bank_statement_helpers as B

    co = await B.company(client)
    bk = await B.bank(client, co["h"])
    await B.statement(client, co, bk["id"], [("2026-01-05", "FEE", "-5.00")], opening="0.00",
                      closing="-5.00", start="2026-01-01", end="2026-01-31")
    lid = B.all_lines(await B.groups(client, co, bk["id"]))[0]["id"]
    manager = await B.member(client, session, co, "manager")
    r = await client.post(f"{B.API}/bank/{bk['id']}/ask", headers=manager,
                          json={"line_ids": [lid], "note": "", "idempotency_key": B.key()})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["fix"] == {"href": "/settings/general?tab=users", "label_key": "settings.tab_users",
                             "roles": ["owner", "admin"]}
    with _reader("manager"):
        assert refusal_fix(detail) is None
        assert t("error.fix_ask_roles", place=t("settings.tab_users"),
                 roles=f"{t('settings.owner')}, {t('settings.admin')}") in refusal_text(detail)
    with _reader("admin"):
        assert refusal_fix(detail) == {"href": "/settings/general?tab=users", "label": t("settings.tab_users")}
    # The company's own grants decide who can: with company settings left to the owner, an
    # admin reads that the owner can, not a link.
    p = await client.patch("/companies/me/role-permissions", headers=co["h"],
                           json={"perm_key": "manage_company_settings", "role_key": "admin", "granted": False})
    assert p.status_code == 200, p.text
    r = await client.post(f"{B.API}/bank/{bk['id']}/ask", headers=manager,
                          json={"line_ids": [lid], "note": "", "idempotency_key": B.key()})
    assert r.json()["detail"]["fix"]["roles"] == ["owner"]
    with _reader("admin"):
        assert refusal_fix(r.json()["detail"]) is None
