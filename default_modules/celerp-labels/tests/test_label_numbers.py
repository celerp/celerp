# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT
"""Every size and position a label template stores goes through one parser. Seeded draws over
the input space (numbers, numeric text, words, blanks, signs, exponents, other digit systems,
huge and non-finite values) go through the API and the settings editor: neither ever answers
5xx, a value is accepted exactly when an independent reference says it is a decimal number
inside its range, and what is stored is that number. A template that already holds a bad value
(stored before the numbers were checked) is refused with a plain reason on every print path."""
from __future__ import annotations

import math
import os
import random
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from celerp_labels.models import LabelTemplate
from celerp_labels.service import FIELD_NUMBERS, LABEL_NUMBERS
from ui.i18n import t

SEED = int(os.environ.get("LABEL_NUMBERS_SEED", "20261112"))
DRAWS = 24
_REFERENCE_CHARS = set("0123456789.+-eE")
_WORDS = ["", " ", "abc", "nan", "NaN", "inf", "-inf", "Infinity", "1_0", "١٢", "๑๒",
          "1e400", "-1e400", "1e", ".", "--5", "5..0", "0x10", "1,5", "12mm", " 5 ", "1e-400",
          "+.5", "5.", "1E1", " 10 ", "1 0", "\t30\n", "５", "²"]


def _reference_value(name: str, value):
    """The number ``value`` means for ``name``, or None when it is not one inside the range:
    decided apart from the parser under test (character set, then float(), then the range)."""
    _, lo, hi = LABEL_NUMBERS[name]
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if abs(value) > 10**308:
            return None
        number = float(value)
    elif isinstance(value, float):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or not set(text) <= _REFERENCE_CHARS:
            return None
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) and lo <= number <= hi else None


def _draw(rng: random.Random, name: str):
    """One value from the input space around ``name``'s range."""
    _, lo, hi = LABEL_NUMBERS[name]
    near = rng.choice([lo, hi, rng.uniform(lo, hi)]) + rng.choice([0, 0, -0.5, 0.5, -1e-9, 1e-9, -lo - 1, hi])
    kind = rng.randrange(8)
    if kind == 0:
        return round(near, rng.randrange(4))
    if kind == 1:
        return int(near)
    if kind == 2:
        return rng.choice([f"{near}", f"{near:.2f}", f"{near:e}", f" {near:g} ", f"+{near:g}", f"{near:G}"])
    if kind == 3:
        return rng.choice(_WORDS)
    if kind == 4:
        return rng.choice([10**400, -(10**400), 10**308, True, False, [near], {"v": near}])
    if kind == 5:
        return str(int(near))
    if kind == 6:
        return rng.choice(["-", "+", ""]) + str(rng.randrange(0, 2000))
    return rng.choice([lo, hi, lo - 1, hi + 1, -lo, 0, -0.0])


async def _register(client: AsyncClient) -> str:
    uid = uuid.uuid4().hex[:8]
    r = await client.post("/auth/register", json={
        "email": f"labelnum_{uid}@example.com", "password": "pass1234",
        "name": "Label Numbers", "company_name": f"LabelNum_{uid}",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _sent(name: str, value) -> dict:
    """A template body carrying ``value`` as ``name``."""
    if name in FIELD_NUMBERS:
        return {"fields": [{"key": "name", "label": "Name", "type": "text", name: value}]}
    other = "height_mm" if name == "width_mm" else "width_mm"
    return {"format": "custom", name: value, other: 50}


def _stored(template: dict, name: str):
    return template["fields"][0].get(name) if name in FIELD_NUMBERS else template.get(name)


@pytest.mark.asyncio
async def test_api_accepts_a_label_number_exactly_when_the_reference_does(client: AsyncClient):
    headers = {"Authorization": f"Bearer {await _register(client)}"}
    rng = random.Random(SEED)
    made = await client.post("/api/labels/templates", json={"name": "Base"}, headers=headers)
    assert made.status_code == 201, made.text
    tid = made.json()["id"]
    for name in LABEL_NUMBERS:
        for _ in range(DRAWS):
            value = _draw(rng, name)
            blank = value is None or value == ""
            expected = _reference_value(name, value)
            for method, url, ok in (("POST", "/api/labels/templates", 201),
                                    ("PUT", f"/api/labels/templates/{tid}", 200)):
                body = {"name": "Draw", **_sent(name, value)}
                r = await client.request(method, url, json=body, headers=headers)
                case = f"seed={SEED} {method} {name}={value!r}"
                assert r.status_code < 500, case
                if blank:
                    assert r.status_code == ok, case
                elif expected is None:
                    assert r.status_code == 422, case
                    assert isinstance(r.json()["detail"], (str, list)), case
                else:
                    assert r.status_code == ok, (case, r.text)
                    assert _stored(r.json(), name) == expected, case
                    if name in FIELD_NUMBERS and float(expected).is_integer():
                        # a whole number reads back as typed: 22, never 22.0
                        assert type(_stored(r.json(), name)) is int, case


@pytest.mark.asyncio
async def test_settings_editor_saves_a_label_number_exactly_when_the_reference_does(client: AsyncClient):
    from ui.app import app as ui_app
    from ui.config import COOKIE_NAME

    token = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    tid = (await client.post("/api/labels/templates", json={"name": "Editor"}, headers=headers)).json()["id"]

    @asynccontextmanager
    async def _api(*_a, **_k):  # the editor's API calls reach the in-process API
        yield client

    rng = random.Random(SEED + 1)
    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
        ui.cookies.set(COOKIE_NAME, token)
        with patch("celerp_labels.ui_routes.httpx.AsyncClient", _api):
            for name in ("width_mm", "height_mm", "x", "fontSize"):
                for _ in range(DRAWS):
                    value = _draw(rng, name)
                    typed = value if isinstance(value, str) else str(value)
                    before = (await client.get(f"/api/labels/templates/{tid}", headers=headers)).json()
                    form = {"name": "Editor", "format": "custom", "width_mm": "50", "height_mm": "30",
                            "fields[0][key]": "name", "fields[0][label]": "Name", "fields[0][type]": "text"}
                    form[f"fields[0][{name}]" if name in FIELD_NUMBERS else name] = typed
                    r = await ui.put(f"/settings/labels/{tid}", data=form, headers={"HX-Request": "true"})
                    case = f"seed={SEED + 1} {name}={typed!r}"
                    assert r.status_code < 500, case
                    after = (await client.get(f"/api/labels/templates/{tid}", headers=headers)).json()
                    expected = _reference_value(name, typed)
                    if not typed.strip():
                        assert "error" not in r.headers.get("HX-Trigger", ""), case
                    elif expected is None:
                        assert "error" in r.headers.get("HX-Trigger", ""), case
                        assert after == before, case
                    else:
                        assert "error" not in r.headers.get("HX-Trigger", ""), (case, r.headers)
                        assert _stored(after, name) == expected, case


_BAD_STORED = [
    {"fields": [{"key": "name", "type": "text", "x": "abc"}]},
    {"fields": [{"key": "name", "type": "text", "fontSize": "NaN"}]},
    {"fields": [{"key": "name", "type": "barcode", "barcode_height": -4}]},
    {"fields": [{"key": "name", "type": "text", "y": 1e300}]},
    {"format": "custom", "width_mm": -5.0, "height_mm": 30.0},
    {"format": "custom", "width_mm": 50.0, "height_mm": 1e300},
    {"format": "0x30mm"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", _BAD_STORED, ids=[str(i) for i in range(len(_BAD_STORED))])
async def test_a_stored_bad_number_is_refused_plainly_on_every_print_path(client: AsyncClient, session, bad):
    from ui.app import app as ui_app
    from ui.config import COOKIE_NAME

    token = await _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    tid = (await client.post("/api/labels/templates", json={"name": "Old"}, headers=headers)).json()["id"]
    await session.execute(sa.update(LabelTemplate).where(LabelTemplate.id == uuid.UUID(tid)).values(**bad))
    await session.flush()

    r = await client.post(f"/api/labels/print/item-1?template_id={tid}", headers=headers)
    assert r.status_code == 422, r.text
    assert r.json()["detail"].startswith("This label template cannot be printed.")
    r = await client.post("/api/labels/bulk-print", json={"template_id": tid, "entity_ids": ["item-1"]},
                          headers=headers)
    assert r.status_code == 422, r.text

    @asynccontextmanager
    async def _api(*_a, **_k):
        yield client

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
        ui.cookies.set(COOKIE_NAME, token)
        with patch("celerp_labels.ui_routes.httpx.AsyncClient", _api):
            page = await ui.get(f"/labels/print/item-1?template_id={tid}")
    assert page.status_code == 422, page.text[:300]
    assert t("labels.cannot_print", reason="").strip() in page.text
    assert "window.print" not in page.text


@pytest.mark.asyncio
@pytest.mark.parametrize("height", ["abc", "-3", "1e400", "nan", "", "99"])
async def test_barcode_preview_refuses_a_bad_height_plainly(client: AsyncClient, height):
    from ui.app import app as ui_app

    async with AsyncClient(transport=ASGITransport(app=ui_app), base_url="http://ui") as ui:
        r = await ui.get("/api/labels/preview/barcode", params={"value": "12345", "height": height})
    assert r.status_code == 422, r.text
    assert "Bar height (mm) must be a number from 1 to 30." == r.text
