"""A double-click editor on the manufacturing pages prefills the value its cell shows, whatever
characters it holds: the cell's address carries the value encoded, so a work centre named
"R&D #1" opens with "R&D #1", not "R" (which leaving the field would then save)."""
from __future__ import annotations

import re
from html import unescape

import pytest
from fasthtml.common import FastHTML, to_xml
from starlette.testclient import TestClient

import ui.routes.manufacturing as mfg

NAMES = ["R&D #1", "50% + tax", "a=b&current=x", "Line?2"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(mfg, "_token", lambda request: "token")
    app = FastHTML()
    mfg.setup_routes(app)
    return TestClient(app)


def _hx_get(node) -> str:
    return unescape(re.search(r'hx-get="([^"]+)"', to_xml(node)).group(1))


@pytest.mark.parametrize("name", NAMES)
def test_work_centre_name_editor_prefills_the_whole_name(client, name):
    r = client.get(_hx_get(mfg._wc_cell("wc-1", "name", name)))
    assert r.status_code == 200
    field = re.search(r'<input[^>]*name="value"[^>]*>', r.text).group(0)
    assert unescape(re.search(r'\svalue="([^"]*)"', field).group(1)) == name
    assert unescape(re.search(r'data-esc-restore="([^"]*)"', field).group(1)) == name
