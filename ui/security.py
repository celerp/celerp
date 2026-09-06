# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Shared UI security predicates."""

from __future__ import annotations

from urllib.parse import urlparse


def is_app_local_path(path) -> bool:
    """True only for a non-empty, single-leading-slash, in-app relative path.

    Rejects any scheme (`https:`, `javascript:`) and protocol-relative `//host`,
    so a redirect target or a link built from untrusted input (a `?next=` value,
    a third-party search provider's row) can only stay inside this app, never
    bounce to an external site or execute a script URL. A backslash is rejected
    too: browsers normalise `\\` to `/`, so `/\\evil.example` resolves off-site
    just like `//evil.example`. ASCII control chars (0x00-0x1F, 0x7F) are
    rejected as never-legitimate and unsafe to place in a link.

    This is the single source of truth for app-local path safety. `celerp`
    (the API layer) cannot import this UI-layer module, so `_local_href` in
    `celerp/routers/search.py` mirrors this exact rule; the two must change
    together.
    """
    return (
        isinstance(path, str)
        and path.startswith("/")
        and not path.startswith("//")
        and "\\" not in path
        and not any(ord(c) < 0x20 or ord(c) == 0x7F for c in path)
    )


def is_safe_authorize_url(url: str) -> bool:
    """A broker-supplied OAuth authorize URL is safe to open/inject only if it is https and
    carries nothing that could break out of a <script> tag or href (angle brackets / control
    chars) or use a non-web scheme (javascript:/data:)."""
    return (
        bool(url)
        and urlparse(url).scheme == "https"
        and not any(c in url for c in "<>")
        and not any(ord(c) < 0x20 for c in url)
    )
