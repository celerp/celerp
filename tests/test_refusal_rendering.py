# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A refused line action reads as plain sentences in the user's language: never a JSON
payload, never an internal record id, never an empty toast."""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from celerp.accounting_roles import refusal
from test_ui import _authed, ui_client  # noqa: F401  (ui_client is a fixture)
from ui import i18n
from ui.api_client import _api_error

_UUID = "9f2c41d0-5b7e-4a39-9c61-2f0d8e7a1b34"
_CATALOGS = Path(__file__).resolve().parents[1] / "ui" / "locales"
_RAW = re.compile(r"[{}\[\]]|item:|doc:|[0-9a-f]{8}-[0-9a-f]{4}-", re.I)


def _catalog(lang: str) -> dict:
    return json.loads((_CATALOGS / f"{lang}.json").read_text(encoding="utf-8"))


def _nested() -> dict:
    """The body a per-line action refuses with: one structured refusal, one plain line."""
    return {"detail": {"errors": [
        refusal("item.draft", "RAW-1 is a draft, not stock yet: make it available first.", sku="RAW-1"),
        f"item:{_UUID} (RAW-2): reserved by another document",
    ]}}


@pytest.fixture
def lang():
    yield i18n.set_lang
    i18n.set_lang("en")


# --- refusal_text ------------------------------------------------------------

@pytest.mark.parametrize("code", ["en", "es"])
def test_nested_errors_render_every_line_without_ids(lang, code):
    lang(code)
    text = i18n.refusal_text(_nested()["detail"])
    assert _catalog(code)["item.draft"].format(sku="RAW-1") in text
    assert "RAW-2: reserved by another document" in text
    assert not _RAW.search(text), text


def test_bare_and_prefixed_ids_are_stripped_from_plain_text():
    assert i18n.refusal_text(f"Line {_UUID} cannot be reserved") == "Line cannot be reserved"
    assert i18n.refusal_text(f"doc:{_UUID}: already closed") == "already closed"


def test_an_unrecognised_dict_renders_as_nothing_rather_than_raw():
    assert i18n.refusal_text({"weird": {"x": 1}}) == ""


# --- _api_error --------------------------------------------------------------

def test_nested_errors_keep_their_payload_and_render_plain():
    e = _api_error(422, _nested(), json.dumps(_nested()))
    assert e.data == _nested()["detail"]
    assert isinstance(e.detail, str) and not _RAW.search(e.detail), e.detail


@pytest.mark.parametrize("code", ["en", "es"])
def test_a_non_json_error_reads_as_a_plain_sentence(lang, code):
    lang(code)
    e = _api_error(502, None, "<html><body>Bad Gateway</body></html>")
    assert e.detail == _catalog(code)["error.unexpected_error_body"]


# --- the line action proxies -------------------------------------------------

_ACTIONS = [
    ("post", "/docs/doc:SO-1/reserve-lines", "reserve_lines", {"selected": "item:a"}),
    ("post", "/lists/doc:LS-1/reserve-lines", "reserve_lines", {"selected": "item:a"}),
    ("post", "/docs/doc:SO-1/fulfill-lines", "fulfill_lines", {"selected": "item:a"}),
    ("post", "/docs/doc:SO-1/revert-lines", "unfulfill_lines", {"selected": "item:a"}),
    ("post", "/docs/doc:PO-1/receive", "receive_po",
     {"location_id": "loc:1", "item_id_0": "item:a", "sku_0": "RAW-1", "qty_0": "1"}),
    ("post", "/docs/doc:CN-1/receive-return", "receive_return",
     {"items[0][sku]": "RAW-1", "items[0][quantity]": "1"}),
    ("delete", "/docs/doc:CN-1/receive-return", "undo_receive_return", None),
]


async def _toast(ui_client, method, path, api_fn, form, code, error) -> str:
    with patch(f"ui.api_client.{api_fn}", new=AsyncMock(side_effect=error)):
        kwargs = {"cookies": {**_authed(), "celerp_lang": code}}
        if form is not None:
            kwargs["data"] = form
        r = await getattr(ui_client, method)(path, **kwargs)
    assert r.status_code == 200, r.text
    toast = json.loads(r.headers["HX-Trigger"])["celerpToast"]
    assert toast["type"] == "error"
    return toast["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["en", "es"])
@pytest.mark.parametrize("method,path,api_fn,form", _ACTIONS)
async def test_a_refused_line_action_shows_every_reason_plainly(ui_client, code, method, path, api_fn, form):
    error = _api_error(422, _nested(), json.dumps(_nested()))
    message = await _toast(ui_client, method, path, api_fn, form, code, error)
    assert _catalog(code)["item.draft"].format(sku="RAW-1") in message
    assert "RAW-2" in message
    assert not _RAW.search(message), message


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,api_fn,form", _ACTIONS)
async def test_an_unreadable_refusal_is_never_an_empty_or_raw_toast(ui_client, method, path, api_fn, form):
    error = _api_error(422, {"detail": {"weird": {"x": 1}}}, '{"detail": {"weird": {"x": 1}}}')
    message = await _toast(ui_client, method, path, api_fn, form, "es", error)
    assert message == _catalog("es")["error.unexpected_error_body"]
