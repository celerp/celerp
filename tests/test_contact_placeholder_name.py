"""A new contact starts with a placeholder name in the reader's language, which the page then
offers to edit and the list keeps at the top until it is renamed."""
from __future__ import annotations

import pytest

from test_helpers import perm_setup, ui_get, ui_request
from ui.i18n import t

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("contact_type, key", [("customer", "contacts.new_customer"),
                                               ("vendor", "contacts.new_vendor")])
async def test_a_new_contact_is_named_in_the_readers_language(client, session, contact_type, key):
    ctx = await perm_setup(client, session)
    made = await ui_request(ctx["admin_h"], "POST", f"/contacts/create?type={contact_type}", lang="es")
    contact_path = made.headers["HX-Redirect"]
    page = await ui_get(ctx["admin_h"], contact_path, lang="es")
    assert t(key, "es") in page.text and "New " + contact_type.title() not in page.text
    assert "document.querySelector('.cell--clickable')?.click();" in page.text  # opens on the name
    again = await ui_request(ctx["admin_h"], "POST", f"/contacts/create?type={contact_type}", lang="es")
    assert again.headers["HX-Redirect"] == contact_path  # one placeholder at a time
