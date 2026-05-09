# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Tests for celerp/ai/page_count.py.

Covers:
  - count_pages: PDF (real + corrupt), images, unknown file types
  - credits_for_pages: boundary cases
  - calculate_credits: no files (pure text), single file, multiple files
"""

from __future__ import annotations

import io
import math
import struct
import pytest

from celerp.ai.page_count import (
    _BYTES_PER_PAGE,
    calculate_credits,
    count_pages,
    credits_for_pages,
)


# ── Helpers to build minimal valid files ─────────────────────────────────────

def _make_pdf(num_pages: int) -> bytes:
    """Build a minimal valid PDF with exactly num_pages pages."""
    import pypdf
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _minimal_png() -> bytes:
    """Return a 1×1 white PNG."""
    import struct, zlib
    def chunk(ctype: bytes, data: bytes) -> bytes:
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat_data = zlib.compress(b"\x00\xFF\xFF\xFF")
    idat = chunk(b"IDAT", idat_data)
    iend = chunk(b"IEND", b"")
    return header + ihdr + idat + iend


# ── count_pages: PDF ──────────────────────────────────────────────────────────

def test_count_pages_pdf_single():
    data = _make_pdf(1)
    assert count_pages(data, "application/pdf") == 1


def test_count_pages_pdf_five():
    data = _make_pdf(5)
    assert count_pages(data, "application/pdf") == 5


def test_count_pages_pdf_twenty():
    data = _make_pdf(20)
    assert count_pages(data, "application/pdf") == 20


def test_count_pages_pdf_corrupt_raises():
    with pytest.raises(ValueError, match="Cannot read PDF"):
        count_pages(b"not a pdf at all", "application/pdf")


# ── count_pages: images ───────────────────────────────────────────────────────

def test_count_pages_jpeg():
    # Any bytes are fine — images always return 1
    assert count_pages(b"fake jpeg data", "image/jpeg") == 1


def test_count_pages_png():
    assert count_pages(_minimal_png(), "image/png") == 1


def test_count_pages_gif():
    assert count_pages(b"GIF89a", "image/gif") == 1


def test_count_pages_webp():
    assert count_pages(b"RIFF fake", "image/webp") == 1


# ── count_pages: unknown types ────────────────────────────────────────────────

def test_count_pages_unknown_small():
    """Small unknown file → at least 1 page."""
    data = b"small file"
    assert count_pages(data, "text/plain") == 1


def test_count_pages_unknown_exact_one_page():
    data = b"x" * _BYTES_PER_PAGE
    assert count_pages(data, "application/octet-stream") == 1


def test_count_pages_unknown_just_over_one_page():
    data = b"x" * (_BYTES_PER_PAGE + 1)
    assert count_pages(data, "application/octet-stream") == 2


def test_count_pages_unknown_three_pages():
    data = b"x" * (_BYTES_PER_PAGE * 3)
    assert count_pages(data, "application/zip") == 3


def test_count_pages_never_zero():
    """Even empty data returns at least 1."""
    assert count_pages(b"", "application/octet-stream") == 1


# ── credits_for_pages ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("pages,expected_credits", [
    (1, 1),
    (4, 1),
    (5, 1),
    (6, 2),
    (10, 2),
    (11, 3),
    (25, 5),
    (26, 6),
    (100, 20),
])
def test_credits_for_pages(pages, expected_credits):
    assert credits_for_pages(pages) == expected_credits


# ── calculate_credits ─────────────────────────────────────────────────────────

def test_calculate_credits_no_files():
    """Pure text query = 1 credit."""
    assert calculate_credits([]) == 1


def test_calculate_credits_single_image():
    """1 image file (1 page) = 1 credit."""
    assert calculate_credits([1]) == 1


def test_calculate_credits_five_images():
    """5 image files (1 page each) = 5 credits."""
    assert calculate_credits([1, 1, 1, 1, 1]) == 5


def test_calculate_credits_mixed():
    """3 files: 1 page, 5 pages, 10 pages → 1 + 1 + 2 = 4 credits."""
    assert calculate_credits([1, 5, 10]) == 4


def test_calculate_credits_large_pdf():
    """Single 25-page PDF → 5 credits."""
    assert calculate_credits([25]) == 5


def test_calculate_credits_100_receipts():
    """100 single-page images = 100 credits."""
    assert calculate_credits([1] * 100) == 100
