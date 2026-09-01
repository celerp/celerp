# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The new memo-close copy keys are localized in every release-complete locale:
t("doc.closed") must resolve to a real translation, not the bare key, for each
locale that ships as complete."""

from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from ui.i18n import t

# Kept in lockstep with tests/test_i18n.py::_COMPLETE_LOCALES.
_COMPLETE_LOCALES = ("en", "am", "ar", "de", "es", "fr", "id", "it", "ja", "pt", "th", "vi")

_NEW_KEYS = ("doc.closed", "btn.reopen", "documents.line_label_returned",
             "documents.line_label_not_shipped", "documents.status_memo_out",
             "enum.item_status.sold")


def test_close_button_copy_localized():
    for code in _COMPLETE_LOCALES:
        for key in _NEW_KEYS:
            val = t(key, code)
            assert val and val != key, f"{key} not localized for {code}: {val!r}"
