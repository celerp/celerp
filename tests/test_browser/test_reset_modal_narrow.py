"""On a phone, in German, the company reset modal keeps both of its first-step buttons
inside the modal instead of pushing one past its edge."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_reset_modal_buttons_stay_inside_it_on_a_phone(page, ui_server, fresh_company, lang):
    page.context.add_cookies([{"name": "celerp_lang", "value": lang, "url": ui_server}])
    page.set_viewport_size({"width": 390, "height": 900})
    page.goto(f"{ui_server}/settings/general?tab=company")
    page.locator(".btn--danger.btn--outline").first.click()
    actions = page.locator("#company-reset-step1 .modal-dialog__actions")
    actions.wait_for()

    box = actions.bounding_box()
    buttons = [b.bounding_box() for b in actions.locator(".btn").all()]

    assert len(buttons) == 2
    outside = [b for b in buttons if b["x"] < box["x"] - 0.5 or b["x"] + b["width"] > box["x"] + box["width"] + 0.5]
    assert not outside, (box, buttons)
