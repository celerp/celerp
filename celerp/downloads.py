# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Content-Disposition headers for downloads named after user text."""
from __future__ import annotations

from urllib.parse import quote


def content_disposition(kind: str, filename: str) -> str:
    """The Content-Disposition for serving ``filename`` as ``kind`` (inline or attachment).

    HTTP headers are latin-1, and names here come from user text (document numbers)
    in any script. A name that is not plain URL-safe text is sent as RFC 5987
    ``filename*``, the same rule Starlette's FileResponse applies."""
    quoted = quote(filename)
    if quoted != filename:
        return f"{kind}; filename*=utf-8''{quoted}"
    return f'{kind}; filename="{filename}"'
