# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""A refused API call shown on a page keeps the link to the page that resolves it.

Every place that renders a refused call through refusal_line (directly or through
error_flash) is listed by a scan of the source, and each is driven through its real route
with the API call refusing. The test fails when a place is not driven or its page loses the
link, so a new place is covered the day it is added.
"""

from __future__ import annotations

import ast
import json
import sys
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from celerp.accounting_roles import refusal
from shipped_sources import REPO, shipped_sources, ui_sources
from test_helpers import make_test_token
from ui.api_client import APIError
from ui.i18n import refusal_text

_SHOWN_BY = ("error_flash", "refusal_line")
_REFUSAL = refusal("reconciliation.no_client_user", "")  # as the API answers it, with its fix
_FIX = 'href="/settings/general?tab=users"'

_BANK = {"id": "b1", "bank_name": "Main Bank", "account_number": "****1234", "currency": "USD",
         "bank_type": "checking", "chart_account_code": "1111", "balance": 0.0, "opening_balance": 0.0,
         "is_active": True}
_LINE = {"id": "l1", "statement_id": "s1", "line_date": "2026-01-05", "amount": -200.0, "description": "WITHDRAWAL",
         "reference": None, "raw_balance": 800.0, "counterparty_account": None, "status": "unmatched",
         "matched_je_ids": [], "client_query": None, "attachments": [], "suggestion": None}
_TABLE = {"groups": [{"key": "SPARSE:WITHDRAWAL:out", "direction": "out", "count": 1, "total": -200.0,
                      "ticked": False, "suggestion": None, "lines": [_LINE]}],
          "summary": {"lines": 1, "open": 1, "posted": 0}, "statements": [], "suspense_count": 0,
          "difference": {"amount": 0.0, "notes": []}}
_PREVIEW = {"job_id": "j1", "bank_id": "b1", "receipts": [], "failed_steps": [], "statements": [{
    "file_id": "f1", "header": {"currency": "USD"}, "lines": [], "tie": {"ok": False}, "blocking": [],
    "notices": []}]}
_COMPANY = {"id": "c1", "name": "TestCo", "currency": "USD", "settings": {"currency": "USD"},
            "current_role": "owner"}
_STATE = json.dumps({"headers": [{}], "column_map": None})


def _refused(*_a, **_k):
    raise APIError(422, refusal_text(_REFUSAL), _REFUSAL)


def _sites() -> set[tuple[str, int]]:
    """(file, line) of every call that renders a refused call, outside tests and the helpers."""
    found = set()
    for path in sorted({path for _rel, path in shipped_sources()} | {p for p in ui_sources() if "tests" not in p.parts}):
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _SHOWN_BY
                    and rel != "ui/components/shell.py"):
                found.add((rel, node.lineno))
    return found


_BANK_API = {"get_bank_account": _BANK, "get_bank_accounts": {"items": [_BANK]}, "bank_groups": _TABLE,
             "get_chart": {"items": []}, "get_company": _COMPANY}

# (name, method, url, form, {api function: value or _refused}, cookies)
_CASES = [
    ("statement preview", "get", "/accounting/reconcile/bank/b1/preview/j1", None,
     {"statement_preview": _refused}, {}),
    ("statement preview edit", "post", "/accounting/reconcile/bank/b1/preview/j1",
     {"state": _STATE, "idx": "0", "field": "", "value": ""}, {"statement_preview": _refused}, {}),
    ("statement import", "post", "/accounting/reconcile/bank/b1/preview/j1/import",
     {"state": _STATE, "idempotency_key": "k1"}, {"import_statement": _refused, "statement_preview": _PREVIEW}, {}),
    ("post ticked", "post", "/accounting/reconcile/bank/b1/post-ticked",
     {"group_key": "SPARSE:WITHDRAWAL:out", "idempotency_key": "k1"}, {**_BANK_API, "bank_action": _refused}, {}),
    ("undo ticked", "post", "/accounting/reconcile/bank/b1/unmatch",
     {"line_id": "l1", "view": "all", "idempotency_key": "k1"}, {**_BANK_API, "bank_action": _refused}, {}),
    ("undo one line", "post", "/accounting/reconcile/bank/b1/unmatch",
     {"line_id": "l1", "idempotency_key": "k1"}, {**_BANK_API, "bank_action": _refused}, {}),
    ("ask the client", "post", "/accounting/reconcile/bank/b1/ask",
     {"line_id": "l1", "note": "what is this", "idempotency_key": "k1"}, {**_BANK_API, "bank_action": _refused}, {}),
    ("answer a question", "post", "/accounting/questions/l1/answer",
     {"answer": "rent", "idempotency_key": "k1"}, {"answer_bank_question": _refused, "bank_questions": {"items": []}},
     {}),
    ("hand over ownership, reading users", "post", "/settings/users/u2/installation-owner", {},
     {"get_users": _refused}, {}),
    ("hand over ownership", "post", "/settings/users/u2/installation-owner", {},
     {"get_users": {"items": [{"id": "u2", "name": "Two", "role": "admin", "is_active": True}]},
      "get_company": _COMPANY, "transfer_install_owner": _refused, "installation_owner": True}, {}),
    ("billing portal", "get", "/settings/billing-portal", None,
     {"get_company": _COMPANY, "get_billing_portal_url": _refused, "installation_owner": True}, {}),
    ("side menu, reading the company", "post", "/settings/menu/reset", {}, {"get_company": _refused}, {}),
    ("side menu, saving", "post", "/settings/menu/reset", {},
     {"get_company": _COMPANY, "get_nav_overrides": _refused}, {}),
    ("migration scan", "get", "/setup/new-company/migrate/coverage", None,
     {"migration_scan_read": _refused}, {"celerp_migration_scan": "scan1"}),
    ("payments settings", "get", "/settings/payments", None,
     {"get_company": _COMPANY, "installation_owner": True}, {}),
]


@pytest_asyncio.fixture
async def ui_client():
    from ui.app import app as ui_app
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui", follow_redirects=False) as c:
        yield c


def _recording(real, module_file: str, hit: set):
    def shown(error, *a, **k):
        hit.add((module_file, sys._getframe(1).f_lineno))
        return real(error, *a, **k)
    return shown


def _recorders(hit: set) -> list:
    """Every module that renders a refused call records the line it renders from into *hit*."""
    out = []
    for rel in sorted({f for f, _ in _sites()}):
        mod_name = rel[:-3].replace("/", ".")
        if rel.startswith("default_modules/"):
            mod_name = ".".join(rel[:-3].split("/")[2:])
        mod = sys.modules.get(mod_name) or __import__(mod_name, fromlist=["_"])
        for name in _SHOWN_BY:
            if hasattr(mod, name):
                out.append(patch.object(mod, name, _recording(getattr(mod, name), rel, hit)))
    return out


async def _drive(ui_client, case, hit: set):
    _name, method, url, form, api, cookies = case
    patches = [patch(f"ui.api_client.{fn}", AsyncMock(side_effect=v) if v is _refused else AsyncMock(return_value=v))
               for fn, v in api.items()]
    if url == "/settings/payments":
        patches.append(patch("ui.routes.settings_payments._load", AsyncMock(side_effect=_refused)))
        patches.append(patch("ui.routes.settings_payments._documents_off", AsyncMock(return_value=None)))
    if url.startswith("/setup/new-company/migrate"):
        patches.append(patch("ui.routes.migrations.gate", AsyncMock(return_value=None)))
    patches += _recorders(hit)
    for p in patches:
        p.start()
    try:
        ui_client.cookies.set("celerp_token", make_test_token(role="owner"))
        for k, v in cookies.items():
            ui_client.cookies.set(k, v)
        return await (ui_client.get(url) if method == "get" else ui_client.post(url, data=form or {}))
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.mark.asyncio
async def test_every_place_that_shows_a_refused_call_keeps_the_link_to_its_fix(ui_client):
    """Each case keeps the link, and the cases together reach every place that shows a
    refused call, in one test so no split of the suite can run the check without its cases."""
    hit: set[tuple[str, int]] = set()
    lost = {}
    for case in _CASES:
        r = await _drive(ui_client, case, hit)
        if _FIX not in r.text:
            lost[case[0]] = (r.status_code, r.text[:300])
    assert lost == {}
    assert _sites() - hit == set()
