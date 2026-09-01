# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""The Portuguese and Spanish memo close/reopen copy uses each locale's own memo
and inventory terminology (pt: memorando / estoque; es: nota / inventario), not the
European or untranslated wording that shipped first."""

from __future__ import annotations

import os
os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

from ui.i18n import t

# The Close/Reopen tips and the reopen confirmation are the memo-terminology copy.
_MEMO_COPY_KEYS = ("documents.tip_close_memo", "documents.tip_reopen_memo",
                   "documents.reopen_memo_confirm")


def _copy(code: str) -> str:
    return " ".join(t(k, code).lower() for k in _MEMO_COPY_KEYS)


def test_pt_memo_terminology():
    """Portuguese uses "memorando" for the memo and "estoque" for inventory, and no
    longer uses the European "stock" or the untranslated "memo"."""
    blob = _copy("pt")
    assert "memorando" in blob, f"pt memo copy must use 'memorando'; got {blob!r}"
    assert "estoque" in blob, f"pt memo copy must use 'estoque'; got {blob!r}"
    assert "stock" not in blob, f"pt memo copy must not use European 'stock'; got {blob!r}"
    # "memo" as a standalone word (not inside "memorando").
    import re
    assert not re.search(r"\bmemo\b", blob), f"pt memo copy must not use untranslated 'memo'; got {blob!r}"


def test_es_memo_terminology():
    """Spanish uses "nota" for the memo and "inventario" for inventory, and no longer
    uses the untranslated "memo"."""
    blob = _copy("es")
    assert "nota" in blob, f"es memo copy must use 'nota'; got {blob!r}"
    assert "inventario" in blob, f"es memo copy must use 'inventario'; got {blob!r}"
    import re
    assert not re.search(r"\bmemo\b", blob), f"es memo copy must not use untranslated 'memo'; got {blob!r}"


def test_pt_es_memo_terminology():
    """Combined gate: both locales use their own terminology and the JSON resolves
    (t() returns a real translation for every changed key, not the bare key)."""
    for code in ("pt", "es"):
        for k in _MEMO_COPY_KEYS:
            val = t(k, code)
            assert val and val != k, f"{k} not localized for {code}: {val!r}"
    test_pt_memo_terminology()
    test_es_memo_terminology()
