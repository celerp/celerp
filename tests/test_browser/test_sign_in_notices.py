# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A notice on the sign-in card (a password reset, a session that ended) stands apart from the
form under it, spaced like the form's own fields, as a refusal on the same card is."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser

_GAP = """() => { const box = document.querySelector('.auth-card .flash');
    const next = document.querySelector('.auth-card form .form-group, .auth-card form label');
    return next.getBoundingClientRect().top - box.getBoundingClientRect().bottom; }"""


@pytest.mark.parametrize("reason", ["password-reset", "password-changed", "expired", "idle"])
def test_a_sign_in_notice_is_spaced_from_the_form(unauthed_page, ui_server, reason):
    unauthed_page.goto(f"{ui_server}/login?reason={reason}", wait_until="load")
    assert unauthed_page.evaluate(_GAP) >= 8, reason
