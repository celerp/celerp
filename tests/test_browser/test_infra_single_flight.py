# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""The infra Save forms and the partner-claim Review form must single-flight.

Each of these forms carried ``hx-disabled-elt="this"`` on the ``<form>``. A
``<form>`` has no cascading ``disabled``, so the submit button stayed clickable
while a request was in flight: a rapid second submit fired a second
non-idempotent request (a second Save & Restart, or a second claim lookup).

The submit button itself must be disabled for the duration of the request, so a
second submit while the first is outstanding issues no request. These assert the
running behavior under real htmx, not the static markup.

Red at merge-base: the form-level disable leaves the submit button enabled, so
the button never reports ``disabled`` during the request and the second click
issues a second request.
"""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

pytestmark = pytest.mark.browser


def _render_db() -> str:
    from ui.routes.settings_cloud import _infra_db_section
    return to_xml(_infra_db_section())


def _render_storage() -> str:
    from ui.routes.settings_cloud import _infra_storage_section
    return to_xml(_infra_storage_section())


def _render_claim() -> str:
    from ui.routes.settings_cloud import _partner_claim_card
    return to_xml(_partner_claim_card(lang="en"))


_CASES = [
    ("db", _render_db, "/settings/cloud/save-infra", "**/settings/cloud/save-infra"),
    ("storage", _render_storage, "/settings/cloud/save-infra", "**/settings/cloud/save-infra"),
    ("claim", _render_claim, "/settings/partner-claim/resolve",
     "**/settings/partner-claim/resolve"),
]


@pytest.mark.parametrize("name,render,post_path,route_glob", _CASES,
                         ids=[c[0] for c in _CASES])
def test_submit_button_single_flights(page, ui_server, name, render, post_path, route_glob):
    """With the POST held open, the submit button is disabled during the request
    and a rapid second submit issues no second request: exactly one request in
    flight across two clicks."""
    requests: list[str] = []
    page.on("request",
            lambda req: requests.append(req.url) if post_path in req.url else None)
    # The Save forms carry hx-confirm on the submit button; auto-accept so the
    # request proceeds. The claim form has no confirm, so this is a harmless no-op
    # there.
    page.on("dialog", lambda d: d.accept())
    # Hold the POST open so the in-flight (button-disabled) state persists across
    # the second click; released before teardown.
    held: list = []
    page.route(route_glob, lambda route: held.append(route))

    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.htmx")
    # Mount the real form markup and let htmx wire it, exactly as the settings page
    # would after render.
    page.evaluate(
        "html => { document.body.innerHTML = html; window.htmx.process(document.body); }",
        render(),
    )
    page.wait_for_selector("button[type='submit']")

    # First submit.
    page.eval_on_selector("button[type='submit']", "b => b.click()")
    # Let htmx issue the request and apply the in-flight disable.
    page.wait_for_timeout(400)
    assert len(requests) == 1, f"{name}: first submit should issue exactly one request"
    disabled = page.eval_on_selector("button[type='submit']", "b => b.disabled")
    assert disabled is True, (
        f"{name}: submit button must be disabled while the request is in flight")

    # Rapid second submit on the (now disabled) button: a no-op, no new request.
    page.eval_on_selector("button[type='submit']", "b => b.click()")
    page.wait_for_timeout(400)
    assert len(requests) == 1, (
        f"{name}: a second submit while the first is in flight must issue no "
        f"second request, but {len(requests)} were issued")

    # Release the held route so teardown does not block on the outstanding request.
    page.unroute(route_glob)
    for route in held:
        try:
            route.abort()
        except Exception:
            pass
