# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Page counting and credit calculation for AI file processing.

Credit rules (per revenue model):
  - Pure text query (no files): 1 credit
  - Query with files: 0 base + ceil(pages / 5) per file, minimum 1 per file
  - Page counting:
      PDF: actual page count via pypdf
      Images (jpeg, png, gif, webp): 1 page each
      Other files: estimated from file size (1 page per 50KB, min 1)
"""

from __future__ import annotations

import io
import math


_BYTES_PER_PAGE = 50 * 1024  # 50KB per estimated page for unknown types


def count_pages(data: bytes, content_type: str) -> int:
    """Return the page count for a file given its raw bytes and MIME type.

    Raises ValueError if the file cannot be parsed (e.g. corrupt PDF).
    Never returns 0 — minimum is 1.
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
            raise ValueError("PDF reports 0 pages — file may be corrupt.")
        return pages

    if ct in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return 1

    # Unknown type — estimate from size
    estimated = max(1, math.ceil(len(data) / _BYTES_PER_PAGE))
    return estimated


def credits_for_pages(page_count: int) -> int:
    """Return the credit cost for a single file with the given page count.

    1 credit per 5 pages, minimum 1.
    """
    return max(1, math.ceil(page_count / 5))


def calculate_credits(file_page_counts: list[int]) -> int:
    """Return total credits for a list of per-file page counts.

    Pure text (empty list): 1 credit.
    With files: 0 base + sum of per-file credits.
    """
    if not file_page_counts:
        return 1
    return sum(credits_for_pages(p) for p in file_page_counts)
