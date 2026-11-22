# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The kinds of file the assistant takes are the kinds its refusals name and the kinds the
bank page offers to pick: a file the picker offers is never refused, and a refused file is
told the full list of what it could be instead."""

from __future__ import annotations

import re

import pytest
from fasthtml.common import to_xml

from celerp.ai.files import AGENT_UPLOAD_TYPES, UPLOAD_TYPE_BY_SUFFIX, XLSX_CONTENT_TYPE, upload_content_type
from ui.components.bank_groups import drop_zone
from ui.i18n import t

# How the English copy names each accepted kind of file.
_NAMED = {
    "image/jpeg": "image", "image/png": "image", "image/webp": "image",
    "application/pdf": "PDF", "text/csv": "CSV", XLSX_CONTENT_TYPE: ".xlsx",
    "application/x-ofx": ".ofx", "application/vnd.intu.qfx": ".qfx",
}


def test_every_accepted_kind_has_a_name():
    assert set(_NAMED) == set(AGENT_UPLOAD_TYPES)


@pytest.mark.parametrize("key", ["error.ai_file_type", "ai.err_file_type_named"])
def test_a_file_type_refusal_names_every_kind_the_assistant_takes(key):
    text = t(key, "en", name="x.bin")
    assert [kind for kind in sorted(set(_NAMED.values())) if kind not in text] == []


def _accepted(html: str) -> list[str]:
    return re.search(r'accept="([^"]+)"', html).group(1).split(",")


def test_every_kind_the_assistant_takes_has_an_extension():
    assert set(UPLOAD_TYPE_BY_SUFFIX.values()) == set(AGENT_UPLOAD_TYPES)


def test_the_bank_page_offers_only_files_the_assistant_takes():
    refused = [ext for ext in _accepted(to_xml(drop_zone("b1"))) if upload_content_type(f"f{ext}", None) not in AGENT_UPLOAD_TYPES]
    assert refused == []


def test_the_chat_offers_every_kind_the_assistant_takes():
    from celerp_ai.ui_routes import _chat_view

    offered = {upload_content_type(f"f{ext}", None) for ext in _accepted(to_xml(_chat_view()))}
    assert offered == set(AGENT_UPLOAD_TYPES)


@pytest.mark.parametrize("name", ["march.ofx", "march.QFX", "march.csv"])
def test_a_statement_file_sent_with_a_generic_type_is_kept_as_its_kind(name):
    assert upload_content_type(name, "application/octet-stream") in AGENT_UPLOAD_TYPES
