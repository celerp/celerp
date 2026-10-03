# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A production run refused, and the work in progress account, in the user's language.

The API refuses in English with a ``message_key`` and its ``params``; the UI shows the
key's translation filled with those params, and the server's English when no catalog
has the key. Every language the UI ships carries each refusal the owner sees most and
the label of every posting account.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fasthtml.common import to_xml

from celerp.accounting_roles import ROLE_LABELS
from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui import i18n
from ui.api_client import APIError

ROOT = Path(__file__).resolve().parents[1]
LOCALES = sorted(p.stem for p in (ROOT / "ui" / "locales").glob("*.json"))
REFUSALS = ["mfg.reconciliation_required", "mfg.insufficient_stock", "mfg.over_receipt", "mfg.cancel_moved",
            "mfg.wip_account_missing", "mfg.issue_first", "mfg.unaccounted_value"]
SHORT = {"message": "Only 2 of RAW-1 is in stock and not reserved; 4 is needed.",
         "message_key": "mfg.insufficient_stock", "params": {"sku": "RAW-1", "available": 2.0, "needed": 4.0}}
_TH_SHORT = "RAW-1 มีในสต๊อกที่ไม่ได้จองไว้เพียง 2 แต่ต้องใช้ 4"
_TH_CANCEL = "ใบสั่งผลิตนี้ใช้วัตถุดิบหรือผลิตสินค้าไปแล้ว จึงยกเลิกไม่ได้ ให้พักไว้หรือปิดใบสั่งผลิตแทน"


@pytest.fixture(autouse=True)
def _lang():
    yield
    i18n.set_lang("en")


def _catalog(lang: str) -> dict:
    return json.loads((ROOT / "ui" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


def _fields(text: str) -> set[str]:
    return set(re.findall(r"\{(\w+)", text))


def _refusal_keys() -> set[str]:
    """Every message_key the manufacturing movements refuse with."""
    src = (ROOT / "default_modules" / "celerp-manufacturing" / "celerp_manufacturing" / "movements.py").read_text()
    return {f"mfg.{node.args[1].value}" for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "refuse"
            and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)}


def test_the_ui_ships_twelve_languages():
    assert LOCALES == ["am", "ar", "de", "en", "es", "fr", "id", "it", "ja", "pt", "th", "vi"]


@pytest.mark.parametrize("lang", LOCALES)
def test_each_language_words_the_refusals_and_every_posting_account(lang):
    en, catalog = _catalog("en"), _catalog(lang)
    for key in REFUSALS + [f"posting.role.{r.value}" for r in ROLE_LABELS]:
        assert catalog.get(key), f"{lang} has no {key}"
        assert _fields(catalog[key]) == _fields(en[key]), f"{lang} {key} fills different values"
        assert "—" not in catalog[key]


def test_every_translated_refusal_is_one_the_runs_can_give():
    assert set(REFUSALS) <= _refusal_keys()


def test_the_english_catalog_says_what_the_server_says():
    """Where the server's wording is exact, the English catalog is that wording."""
    en = _catalog("en")
    assert en["posting.role.work_in_progress"] == ROLE_LABELS[next(r for r in ROLE_LABELS
                                                                    if r.value == "work_in_progress")]
    assert all(en[f"posting.role.{r.value}"] == label for r, label in ROLE_LABELS.items())
    assert en["mfg.insufficient_stock"].format(**SHORT["params"]) == SHORT["message"]


def test_a_refusal_is_shown_in_the_users_language_with_its_values():
    i18n.set_lang("th")
    assert i18n.refusal_text(SHORT) == _TH_SHORT


def test_a_refusal_no_catalog_knows_is_shown_as_the_server_wrote_it():
    i18n.set_lang("th")
    assert i18n.refusal_text({"message": "Run closed.", "message_key": "mfg.not_a_key", "params": {}}) == "Run closed."
    assert i18n.refusal_text({**SHORT, "params": {"sku": "RAW-1"}}) == SHORT["message"]
    assert i18n.refusal_text("plain detail") == "plain detail"
    assert i18n.refusal_text({"message": "No key."}) == "No key."


def test_the_posting_accounts_panel_names_work_in_progress_in_the_users_language():
    from ui.routes.settings_accounting import _posting_role_row

    i18n.set_lang("de")
    html = to_xml(_posting_role_row({"role": "work_in_progress", "label": "Work in progress", "status": "missing",
                                     "code": None, "name": None, "problem": None, "earlier": []}))
    assert "Unfertige Erzeugnisse" in html and "Work in progress" not in html


@pytest.mark.asyncio
async def test_cancelling_a_run_that_used_materials_says_why_in_the_users_language(ui_client):
    refused = APIError(409, "This run has already used materials or produced output, so it cannot be cancelled.",
                       {"message": "This run has already used materials or produced output, so it cannot be "
                                   "cancelled.", "message_key": "mfg.cancel_moved", "params": {}})
    with (
        patch("ui.api_client.cancel_mfg_order", new=AsyncMock(side_effect=refused)),
        patch("ui.api_client.get_item", new=AsyncMock(return_value={"id": "item:p", "sku": "P"})),
        patch("ui.api_client.get_company", new=AsyncMock(return_value={"currency": "THB"})),
        patch("ui.api_client.manufacturing_item_hub", new=AsyncMock(return_value={})),
    ):
        r = await ui_client.post("/api/items/item:p/runs/mfg:1/act", data={"action": "cancel"},
                                 cookies={**_authed(), "celerp_lang": "th"})
    assert r.status_code == 200, r.text
    assert _TH_CANCEL in r.text


@pytest.mark.asyncio
async def test_a_bulk_action_says_why_each_run_was_skipped(ui_client):
    result = {"done": ["mfg:2"], "skipped": [
        {"id": "mfg:1", "reason": SHORT["message"], "message_key": SHORT["message_key"], "params": SHORT["params"]},
        {"id": "mfg:3", "reason": "not found"}]}
    with (
        patch("ui.api_client.manufacturing_bulk_run_action", new=AsyncMock(return_value=result)),
        patch("ui.api_client.list_mfg_orders", new=AsyncMock(return_value={"items": []})),
    ):
        r = await ui_client.post("/manufacturing/runs/bulk/issue?status=active",
                                 content=b"selected=mfg%3A1&selected=mfg%3A2&selected=mfg%3A3",
                                 headers={"content-type": "application/x-www-form-urlencoded"},
                                 cookies={**_authed(), "celerp_lang": "th"})
    assert r.status_code == 200, r.text
    message = json.loads(r.headers["HX-Trigger"])["celerpToast"]["message"]
    assert _TH_SHORT in message and "not found" in message
