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

    Rejects any scheme (`https:`, `javascript:`), a protocol-relative `//host`,
    a backslash, and ASCII control chars (0x00-0x1F, 0x7F), so a redirect target
    or link (a `?next=` value, a search provider's row, a module's link template)
    stays inside this app.
    """
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in path)
    )
