# Copyright (c) 2026 Noah Severs. All rights reserved.
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Connect account mutations single-flight under real htmx."""
from __future__ import annotations

import pytest
from fasthtml.common import to_xml

pytestmark = pytest.mark.browser


def _claim_panel() -> str:
    from ui.routes.account import account_panel
    return to_xml(account_panel("en", intent="claim", panel_id="cloud-relay-tab"))


@pytest.mark.parametrize(
    "selector,path,glob",
    [
        ("#cloud-connect-btn", "/settings/cloud-activate", "**/settings/cloud-activate"),
        ("form[hx-post='/settings/cloud-send-otp'] button[type='submit']",
         "/settings/cloud-send-otp", "**/settings/cloud-send-otp"),
    ],
)
def test_connect_mutation_button_disables_and_does_not_double_submit(
        page, ui_server, selector, path, glob):
    requests = []
    held = []
    page.on("request", lambda req: requests.append(req.url) if path in req.url else None)
    page.route(glob, lambda route: held.append(route))

    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.htmx")
    page.evaluate(
        "html => { document.body.innerHTML = html; window.htmx.process(document.body); }",
        _claim_panel(),
    )
    if "send-otp" in path:
        page.fill("input[name='claim_email']", "owner@example.test")

    page.eval_on_selector(selector, "b => b.click()")
    page.wait_for_timeout(400)
    assert len(requests) == 1
    assert page.eval_on_selector(selector, "b => b.disabled") is True
    assert float(page.eval_on_selector(
        selector, "b => getComputedStyle(b).opacity")) < 1.0

    page.eval_on_selector(selector, "b => b.click()")
    page.wait_for_timeout(400)
    assert len(requests) == 1

    page.unroute(glob)
    for route in held:
        try:
            route.abort()
        except Exception:
            pass


def test_connect_panel_sync_drops_competing_mutation(page, ui_server):
    connect_requests = []
    link_requests = []
    held = []
    page.on(
        "request",
        lambda req: connect_requests.append(req.url)
        if "/settings/cloud-activate" in req.url
        else link_requests.append(req.url)
        if "/settings/cloud-send-otp" in req.url else None,
    )
    page.route("**/settings/cloud-activate", lambda route: held.append(route))

    page.goto(f"{ui_server}/", wait_until="domcontentloaded")
    page.wait_for_function("() => !!window.htmx")
    page.evaluate(
        "html => { document.body.innerHTML = html; window.htmx.process(document.body); }",
        _claim_panel(),
    )
    page.fill("input[name='claim_email']", "owner@example.test")
    page.eval_on_selector("#cloud-connect-btn", "b => b.click()")
    page.wait_for_timeout(300)
    page.eval_on_selector(
        "form[hx-post='/settings/cloud-send-otp'] button[type='submit']",
        "b => b.click()",
    )
    page.wait_for_timeout(300)

    assert len(connect_requests) == 1
    assert len(link_requests) == 0

    page.unroute("**/settings/cloud-activate")
    for route in held:
        try:
            route.abort()
        except Exception:
            pass
