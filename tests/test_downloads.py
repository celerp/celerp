# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Download names built from user text reach the browser as valid header parameters."""
import re
from urllib.parse import unquote

import pytest

from celerp.downloads import content_disposition

# RFC 5987 attr-char: what may appear unescaped in a filename* value.
ATTR_CHARS = re.compile(r"[A-Za-z0-9!#$&+\-.^_`|~%]*")


@pytest.mark.parametrize("name", ["INV/2026/001_items.csv", "ใบแจ้งหนี้/2026/001.csv", "a b;c\"d.csv"])
def test_a_name_with_reserved_characters_is_sent_fully_escaped(name):
    header = content_disposition("attachment", name)
    prefix = "attachment; filename*=utf-8''"
    assert header.startswith(prefix), header
    value = header.removeprefix(prefix)
    assert ATTR_CHARS.fullmatch(value), value
    assert unquote(value) == name


def test_a_plain_name_is_sent_as_it_is():
    assert content_disposition("inline", "INV-2026-001.pdf") == 'inline; filename="INV-2026-001.pdf"'
