# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every document type reads differently from every other in every language, so a bill
is never shown with the same word as an invoice."""

from __future__ import annotations

import pytest

from ui.i18n import _DOC_TYPE_LABEL_KEYS, available_langs, t


@pytest.mark.parametrize("lang", available_langs())
def test_document_type_labels_are_distinct(lang):
    labels: dict[str, list[str]] = {}
    for doc_type, key in _DOC_TYPE_LABEL_KEYS.items():
        labels.setdefault(t(key, lang).casefold(), []).append(doc_type)
    shared = {label: types for label, types in labels.items() if len(types) > 1}
    assert not shared, shared
