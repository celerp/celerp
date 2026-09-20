# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Informational page counting for AI file processing.

Page counts are presentation metadata only. Credit pricing is defined by the
cloud AI meter and must not be reimplemented here.
"""

from __future__ import annotations

import io
import math


_BYTES_PER_PAGE = 50 * 1024  # 50KB per estimated page for unknown types


def count_pages(data: bytes, content_type: str) -> int:
    """Return the page count for a file given its raw bytes and MIME type.

    Raises ValueError if the file cannot be parsed (e.g. corrupt PDF).
    Never returns 0; minimum is 1.
    """
    ct = content_type.lower()

    if ct == "application/pdf":
        try:
            import pypdf  # noqa: PLC0415
            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = len(reader.pages)
        except Exception as exc:
            raise ValueError(f"Cannot read PDF page count: {exc}") from exc
        if pages < 1:
            raise ValueError("PDF reports 0 pages, the file may be corrupt.")
        return pages

    if ct in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return 1

    # Unknown type: estimate from size
    estimated = max(1, math.ceil(len(data) / _BYTES_PER_PAGE))
    return estimated
