# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""A refusal's way out fits its reader: every refusal one page resolves, read by every
default role, links to that page only for a role that may take the step there, and
otherwise names the roles that can, never a link to a page the reader cannot use."""
from __future__ import annotations

import ast
import json
import re
from contextlib import contextmanager
from pathlib import Path

import pytest
from fasthtml.common import to_xml

from celerp.accounting_roles import REFUSAL_FIXES, REQUEST_COMPANY_SETTINGS, refusal
from celerp.services.auth import ROLE_LEVELS
from celerp.services.permissions import role_has_permission
from shipped_sources import APP_ROOTS
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


def _cookie(role: str) -> str:
    import base64
    payload = base64.urlsafe_b64encode(json.dumps({"role": role}).encode()).decode().rstrip("=")
    return f"celerp_token=e30.{payload}.sig"


@pytest.mark.parametrize(("cookie", "links"), [(None, True), ("owner", True), ("manager", False), ("viewer", False)])
def test_the_ui_judges_the_way_out_by_the_signed_in_reader(cookie, links):
    """The UI reads the reader's role from their session, so a manager who may not add a
    user reads who can, while an owner, or a page with no one signed in, gets the link."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from ui.i18n import I18nMiddleware

    detail = refusal("reconciliation.no_client_user", "")

    async def page(request):
        return JSONResponse({"link": refusal_fix(detail), "text": refusal_text(detail)})

    app = I18nMiddleware(Starlette(routes=[Route("/", page)]))
    headers = {"cookie": _cookie(cookie)} if cookie else {}
    body = TestClient(app).get("/", headers=headers).json()
    assert (body["link"] is not None) is links
    assert (t("settings.owner") in body["text"]) is not links


_ROOT = Path(__file__).resolve().parents[1]
_ROUTE = re.compile(r"^\s*@\w*(?:router|app|rt)\.(?:get|post|put|patch|delete)\(", re.M)
_ROUTER_FILES = sorted(
    str(path.relative_to(_ROOT)) for root in APP_ROOTS for path in (_ROOT / root).rglob("*.py")
    if "tests" not in path.parts and _ROUTE.search(path.read_text(encoding="utf-8")))
# A detail a program reads, never a person, and who reads it.
_READ_BY_A_PROGRAM = {
    "direct_connection_limit": "the sign-in page, which answers it in the reader's language",
    "Invalid refresh token": "the client renewing its session: one neutral answer for every failure",
}


def _details(path: str) -> list[tuple[int, ast.expr | None]]:
    """Every HTTPException a file builds, by line, with the detail it carries."""
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    return [(call.lineno, next((k.value for k in call.keywords if k.arg == "detail"),
                               call.args[1] if len(call.args) > 1 else None))
            for call in ast.walk(tree) if isinstance(call, ast.Call)
            and getattr(call.func, "attr", getattr(call.func, "id", None)) == "HTTPException"]


def _hand_built(path: str) -> list[int]:
    """Every dict a file writes by hand under the key of a refusal one page resolves."""
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "message_key"
            and isinstance(value, ast.Constant) and value.value in REFUSAL_FIXES]


@pytest.mark.parametrize("path", _ROUTER_FILES)
def test_every_refusal_a_router_file_raises_says_why_in_the_reader_language_and_keeps_its_way_out(path):
    """Class guard, one row per file that declares routes: a refusal is never a bare status
    (the reader would get no reason and no next step), never English written where it is
    raised (it goes through the catalog; tests/test_refusal_keys_in_catalog.py proves each
    key), and a refusal one page resolves is built by ``refusal``, which attaches that
    page, never as a dict written by hand."""
    details = _details(path)
    bare = [line for line, detail in details if detail is None]
    english = [line for line, detail in details if isinstance(detail, ast.JoinedStr)
               or isinstance(detail, ast.Constant) and detail.value not in _READ_BY_A_PROGRAM]
    assert (bare, english, _hand_built(path)) == ([], [], []), path


def test_every_detail_read_by_a_program_is_still_raised():
    raised = {detail.value for path in _ROUTER_FILES for _line, detail in _details(path)
              if isinstance(detail, ast.Constant)}
    assert raised == set(_READ_BY_A_PROGRAM)
