"""Every shipped help page's steps match the running UI.

One case per page: each step's capture recipe opens its route on a company filled
with demo data, runs its actions, and the step's selector and the English text of
every label it names must be visible (as text or as a field's placeholder; a hidden
copy elsewhere on the page, such as in a closed menu, does not count). With HELP_CAPTURE_WRITE=1 each step that
declares an image is written as a cropped webp carrying the capture marker, the
only way a help image is made.
"""
from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import random
import threading
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from celerp.help.checks import CAPTURE_MARKER, MAX_IMAGE_BYTES, shipped_pages
from celerp_labels.presets import PRESET_TEMPLATES
from ui.i18n import t

from .conftest import _set_auth_cookie

pytestmark = pytest.mark.browser

_SEED_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "seed_demo.py"
_spec = importlib.util.spec_from_file_location("seed_demo_help_capture", _SEED_PATH)
seed_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_demo)

_WRITE = os.environ.get("HELP_CAPTURE_WRITE") == "1"
_MAX_WIDTH = 1000
_EXIF_DESCRIPTION = 0x010E
_PAGES = [p for p in shipped_pages() if isinstance(p.data, dict)]


@pytest.fixture(scope="module")
def demo_token(api_server, api):
    """A company of its own, filled with the same demo data every run."""
    r = api.post("/companies", json={"name": f"Help capture {uuid.uuid4().hex[:6]}"})
    assert r.status_code in (200, 201), r.text
    token = r.json()["access_token"]

    async def seed():
        async with httpx.AsyncClient(base_url=api_server, timeout=30.0) as client:
            await seed_demo.seed_items(client, token)
            contacts = await seed_demo.seed_contacts(client, token)
            await seed_demo.seed_docs(client, token, contacts, "invoice", 12)
            await seed_demo.seed_docs(client, token, contacts, "purchase_order", 6)
            # The fixed assets register shows nothing until its one-time start is
            # confirmed; help pages for it capture the register in its normal,
            # already-started state.
            r = await client.post("/fixed-assets/api/start", headers={"Authorization": f"Bearer {token}"})
            assert r.status_code in (200, 201), r.text
            # The default label templates are created through their write route, so no
            # page's first read seeds them: which labels page reads first depends on test order.
            for preset in PRESET_TEMPLATES:
                r = await client.post("/api/labels/templates", json=preset,
                                      headers={"Authorization": f"Bearer {token}"})
                assert r.status_code == 201, r.text

    random.seed(20261022)
    # Playwright's sync API keeps its own event loop running on this thread for the whole
    # session (greenlet-trampolined), so asyncio.run() here would always raise "cannot be
    # called from a running event loop". Run the seed coroutine on a fresh thread, same as
    # api_server/ui_server above run their own event loop off the main thread.
    error: list[BaseException] = []

    def _run():
        try:
            asyncio.run(seed())
        except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread below
            error.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return token


@pytest.fixture
def demo_page(page, browser_context, seeded_user, demo_token):
    _set_auth_cookie(browser_context, demo_token)
    yield page
    _set_auth_cookie(browser_context, seeded_user["access_token"])


def _run(page, action: dict) -> None:
    if "click" in action:
        page.locator(action["click"]).first.click()
    elif "fill" in action:
        page.locator(action["fill"]).first.fill(action["value"])
    elif "press" in action:
        page.keyboard.press(action["press"])
    else:
        expect(page.locator(action["wait"]).first).to_be_visible()


def _visible_label(key: str) -> str:
    """The English label as it shows on screen; a label with a {param} is matched up to it."""
    return t(key, "en").split("{", 1)[0].strip()


def _write_image(target, path: Path) -> None:
    from PIL import Image

    with Image.open(io.BytesIO(target.screenshot())) as shot:
        image = shot.convert("RGB")
    if image.width > _MAX_WIDTH:
        image = image.resize((_MAX_WIDTH, round(image.height * _MAX_WIDTH / image.width)))
    exif = Image.Exif()
    exif[_EXIF_DESCRIPTION] = CAPTURE_MARKER
    for quality in (80, 65, 50, 35, 20):
        buf = io.BytesIO()
        image.save(buf, "WEBP", quality=quality, method=6, exif=exif)
        if buf.tell() <= MAX_IMAGE_BYTES:
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


@pytest.mark.parametrize("help_page", _PAGES, ids=[p.data.get("id", p.path.name) for p in _PAGES])
def test_help_capture_every_page(demo_page, help_page):
    for n, step in enumerate(help_page.data["steps"], 1):
        capture = step["capture"]
        response = demo_page.goto(capture["route"])
        assert response is not None and response.ok, f"step {n}: {capture['route']} did not load"
        for action in capture["actions"]:
            _run(demo_page, action)
        target = demo_page.locator(capture["selector"]).first
        expect(target).to_be_visible()
        for key in step["labels"].values():
            label = _visible_label(key)
            if label:
                shown = demo_page.get_by_text(label).or_(demo_page.get_by_placeholder(label))
                expect(shown.filter(visible=True).first).to_be_visible()
        if _WRITE and step["image"]:
            _write_image(target, help_page.help_dir / step["image"])
