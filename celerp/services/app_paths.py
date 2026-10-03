# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""The app-local path rule: the one check for whether a link stays inside Celerp.

It lives in the API layer so that the API (search provider rows, module slot
templates checked by the loader) and the UI (redirect targets, rendered links)
apply the same rule; the UI imports it from here.
"""
from __future__ import annotations


def is_app_local_path(path) -> bool:
    """True only for a non-empty, single-leading-slash, in-app relative path.

    Rejects any scheme (`https:`, `javascript:`) and protocol-relative `//host`,
    so a redirect target or a link built from untrusted input (a `?next=` value,
    a third-party search provider's row, a module's link template) can only stay
    inside this app, never bounce to an external site or execute a script URL. A
    backslash is rejected too: browsers normalise `\\` to `/`, so
    `/\\evil.example` resolves off-site just like `//evil.example`. ASCII control
    chars (0x00-0x1F, 0x7F) are rejected as never-legitimate and unsafe to place
    in a link.
    """
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in path)
    )
