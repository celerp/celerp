# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Render tests for the GitHub-star UI surfaces (footer CTA, supporter badge,
supporter card). Pure render — no server."""
from __future__ import annotations

import httpx
from fasthtml.common import to_xml

from test_helpers import make_test_token

from ui.components.shell import _topbar, base_shell, star_supporter_card


async def test_footer_cta_present_and_gold():
    xml = to_xml(await base_shell(title="t"))
    assert 'id="star-cta"' in xml
    assert "/stars/cta?medium=footer" in xml
    assert "/stars/badge" in xml
    # Gold + bold so it stands out from the grey "Powered by" link.
    seg = xml.split('id="star-cta"')[1][:140]
    assert "#d4af37" in seg


def test_supporter_badge_slot_in_topbar_for_user():
    # The badge slot lives in the user-menu, which renders only when logged in.
    xml = to_xml(_topbar([], user_email="admin@test.local"))
    assert 'id="supporter-badge"' in xml


def test_supporter_card_component():
    xml = to_xml(star_supporter_card())
    assert 'id="star-supporter-card"' in xml
    assert "/stars/cta?medium=dashboard" in xml
    assert "/stars/claim" in xml             # claim handshake link
    assert "/stars/dismiss" in xml
    # Dismiss is an X button (top-right), not a "Maybe later" button.
    assert 'id="star-card-dismiss"' in xml
    assert "Maybe later" not in xml
    assert "×" in xml
    # Header + body are hydration targets — the COPY comes from the relay (single
    # source), so it is NOT in the static render; only the placeholders + pre-line are.
    assert 'id="star-card-headline"' in xml
    assert 'id="star-card-body"' in xml
    assert 'id="star-card-body-2"' in xml     # smaller sub-paragraph (first-100 promise)
    assert 'id="star-card-claim"' in xml      # claim button (hidden by JS once claimed)
    # Claiming a badge is the public listing - there is no separate opt-in checkbox.
    assert "star-card-optin" not in xml
    assert "List me publicly" not in xml
    # The founders-wall link is rendered hidden; JS reveals it (with the relay-derived
    # href) only in the post-claim thank-you state.
    assert 'id="star-card-wall"' in xml
    assert "See the founders wall" in xml
    assert "wall_url" in xml                   # JS reads the wall URL from /stars/badge
    assert "white-space:pre-line" in xml      # renders the relay's "\n\n" paragraph break
    assert "#d4af37" in xml                    # gold star
    assert "font-size:13px" in xml            # the smaller founding-promise text
    # Card also reads the badge so it can switch to the relay's post-claim thank-you.
    assert "/stars/badge" in xml
    assert "thanks_headline" in xml and "{badge}" in xml
    # Hidden until JS hydrates it (non-neutral + not dismissed).
    assert "display:none" in xml.split('id="star-supporter-card"')[1][:90]


def test_card_asks_for_the_dashboard_copy():
    assert "/stars/cta?medium=dashboard" in to_xml(star_supporter_card())


class _SaidBadge:
    """Stands in for the API client: records what the claim posts and answers with a badge."""

    def __init__(self):
        self.posted = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, path, json=None):
        self.posted.append((path, json))
        return httpx.Response(200, json={"badge": {"label": "Supporter <1>"}})


async def test_a_returned_credential_is_saved_only_when_the_user_confirms(monkeypatch):
    """The relay returns by a GET carrying the credential; that GET asks, the POST saves."""
    from ui.app import app as ui_app
    import ui.routes.stars as stars

    said = _SaidBadge()
    monkeypatch.setattr(stars.api, "_local_client", said)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui_app), base_url="http://ui",
                                 follow_redirects=False, cookies={"celerp_token": make_test_token()}) as c:
        r = await c.get("/stars/claim", params={"cred": "a'b<c"})
        assert r.status_code == 200 and said.posted == []
        assert "method='post' action='/stars/claim'" in r.text
        assert "value='a&#x27;b&lt;c'" in r.text
        r = await c.post("/stars/claim", data={"cred": "a'b<c"})
    assert said.posted == [("/stars/badge", {"cred": "a'b<c"})]
    assert r.status_code == 200 and "Supporter &lt;1&gt;" in r.text
