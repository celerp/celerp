# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

from types import SimpleNamespace
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from celerp_docs.sequences import (
    next_doc_ref,
    validate_pattern,
    get_all_sequences,
    update_sequence,
    list_sequence_key,
    DEFAULT_PATTERN,
    _expand_pattern,
)


def _company(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(settings=kwargs)


def test_default_pattern_produces_prefix_yymm_format():
    """Default {PREFIX}-{YY}{MM}-{####} pattern produces e.g. INV-2603-0001."""
    c = _company()
    now = datetime.now(timezone.utc)
    yy = f"{now.year % 100:02d}"
    mm = f"{now.month:02d}"
    ref = next_doc_ref(c, "invoice")
    assert ref == f"INV-{yy}{mm}-0001"
    ref2 = next_doc_ref(c, "invoice")
    assert ref2 == f"INV-{yy}{mm}-0002"


def test_custom_pattern_with_prefix():
    c = _company(sequences={"invoice": {"prefix": "INV", "pattern": "{PREFIX}-{YYYY}-{####}"}})
    now = datetime.now(timezone.utc)
    ref = next_doc_ref(c, "invoice")
    assert ref == f"INV-{now.year}-0001"
    ref2 = next_doc_ref(c, "invoice")
    assert ref2 == f"INV-{now.year}-0002"


def test_monthly_reset():
    """When pattern includes {MM}, counter resets on month change."""
    c = _company(sequences={"invoice": {
        "pattern": "{YY}{MM}-{##}", "next": 5, "year": 2026, "month": 2,
    }})
    now = datetime.now(timezone.utc)
    ref = next_doc_ref(c, "invoice")
    if now.month != 2 or now.year != 2026:
        # Month changed, so counter reset to 1
        assert ref.endswith("-01")
    else:
        assert ref.endswith("-05")


def test_yearly_reset():
    """Pattern without {MM} resets only on year change."""
    c = _company(sequences={"invoice": {
        "pattern": "{YYYY}-{####}", "prefix": "INV", "next": 10, "year": 2025, "month": 12,
    }})
    now = datetime.now(timezone.utc)
    ref = next_doc_ref(c, "invoice")
    assert ref == f"{now.year}-0001"  # Year changed from 2025, reset


def test_per_doc_type_independent_sequences():
    c = _company()
    now = datetime.now(timezone.utc)
    yymm = f"{now.year % 100:02d}{now.month:02d}"
    assert next_doc_ref(c, "invoice") == f"INV-{yymm}-0001"
    assert next_doc_ref(c, "purchase_order") == f"PO-{yymm}-0001"
    assert next_doc_ref(c, "invoice") == f"INV-{yymm}-0002"
    assert next_doc_ref(c, "purchase_order") == f"PO-{yymm}-0002"


def test_unsupported_type_raises():
    c = _company()
    with pytest.raises(ValueError):
        next_doc_ref(c, "bogus")


def test_validate_pattern_requires_sequence_token():
    assert validate_pattern("{PREFIX}-{YYYY}") is not None  # no {##}
    assert "sequence token" in validate_pattern("{PREFIX}-{YYYY}")


def test_validate_pattern_rejects_unknown_tokens():
    err = validate_pattern("{##}-{INVALID}")
    assert err is not None
    assert "Unknown" in err


def test_validate_pattern_accepts_valid():
    assert validate_pattern("{YY}{MM}-{##}") is None
    assert validate_pattern("{PREFIX}-{YYYY}-{####}") is None
    assert validate_pattern("{DD}/{MM}/{YY}-{###}") is None


def test_get_all_sequences_returns_all_types():
    c = _company()
    seqs = get_all_sequences(c)
    doc_types = {s["doc_type"] for s in seqs}
    assert "invoice" in doc_types
    assert "purchase_order" in doc_types
    assert "quotation" in doc_types
    for s in seqs:
        assert "preview" in s
        assert "pattern" in s


def test_update_sequence_changes_prefix():
    c = _company()
    result = update_sequence(c, "invoice", prefix="FAK")
    assert result["prefix"] == "FAK"
    assert c.settings["sequences"]["invoice"]["prefix"] == "FAK"


def test_update_sequence_changes_pattern():
    c = _company()
    result = update_sequence(c, "invoice", pattern="{PREFIX}-{YYYY}-{####}")
    assert result["pattern"] == "{PREFIX}-{YYYY}-{####}"


def test_update_sequence_rejects_invalid_pattern():
    c = _company()
    with pytest.raises(ValueError, match="sequence token"):
        update_sequence(c, "invoice", pattern="{PREFIX}-{YYYY}")


def test_update_sequence_resets_next():
    c = _company(sequences={"invoice": {"next": 50}})
    result = update_sequence(c, "invoice", next_num=1)
    assert result["next"] == 1


def test_update_sequence_min_next_is_1():
    c = _company()
    result = update_sequence(c, "invoice", next_num=-5)
    assert result["next"] == 1


def test_expand_pattern_with_dd():
    result = _expand_pattern("{DD}/{MM}/{YY}-{###}", "X", date(2026, 3, 23), 7)
    assert result == "23/03/26-007"


def test_expand_pattern_hash_padding():
    day = date(2026, 1, 1)
    assert _expand_pattern("{#####}", "X", day, 42) == "00042"
    assert _expand_pattern("{##}", "X", day, 42) == "42"
    assert _expand_pattern("{##}", "X", day, 1) == "01"


@pytest.mark.parametrize("list_type,prefix", [
    ("quotation", "QUO"), ("transfer", "TRF"), ("audit", "AUD"), ("writeoff", "WO"), ("shipping_doc", "SHIP"),
])
def test_each_list_type_has_its_own_counter_and_prefix(list_type, prefix):
    c = _company()
    key = list_sequence_key(list_type)
    assert key == list_type
    assert next_doc_ref(c, key).startswith(f"{prefix}-")


@pytest.mark.parametrize("list_type", [None, "", "sale", "something_new"])
def test_an_untyped_or_unknown_list_is_a_quotation(list_type):
    assert list_sequence_key(list_type) == "quotation"


def test_the_old_shared_list_counter_is_not_drawn_from_by_lists():
    c = _company(sequences={"list": {"prefix": "OLD", "pattern": "{PREFIX}-{####}", "next": 40}})
    assert next_doc_ref(c, list_sequence_key("quotation")).startswith("QUO-")
    assert c.settings["sequences"]["list"]["next"] == 40
