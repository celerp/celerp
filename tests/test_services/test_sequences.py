# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

from __future__ import annotations

from types import SimpleNamespace
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from celerp.accounting_roles import Refused
from celerp_docs.sequences import (
    next_doc_ref,
    require_pattern,
    get_all_sequences,
    update_sequence,
    list_sequence_key,
    DEFAULT_PATTERN,
    _expand_pattern,
)


def _company(**kwargs) -> SimpleNamespace:
    return SimpleNamespace(settings=kwargs)


def _row(c, doc_type: str) -> dict:
    return next(r for r in get_all_sequences(c, ()) if r["doc_type"] == doc_type)


def test_default_pattern_produces_prefix_yymm_format():
    """Default {PREFIX}-{YY}{MM}-{####} pattern produces e.g. INV-2603-0001."""
    c = _company()
    now = datetime.now(timezone.utc)
    yy = f"{now.year % 100:02d}"
    mm = f"{now.month:02d}"
    ref = next_doc_ref(c, "invoice", ())
    assert ref == f"INV-{yy}{mm}-0001"
    ref2 = next_doc_ref(c, "invoice", ())
    assert ref2 == f"INV-{yy}{mm}-0002"


def test_custom_pattern_with_prefix():
    c = _company(sequences={"invoice": {"prefix": "INV", "pattern": "{PREFIX}-{YYYY}-{####}"}})
    now = datetime.now(timezone.utc)
    ref = next_doc_ref(c, "invoice", ())
    assert ref == f"INV-{now.year}-0001"
    ref2 = next_doc_ref(c, "invoice", ())
    assert ref2 == f"INV-{now.year}-0002"


def test_monthly_reset():
    """When pattern includes {MM}, counter resets on month change."""
    c = _company(sequences={"invoice": {
        "pattern": "{YY}{MM}-{##}", "next": 5, "year": 2026, "month": 2,
    }})
    now = datetime.now(timezone.utc)
    ref = next_doc_ref(c, "invoice", ())
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
    ref = next_doc_ref(c, "invoice", ())
    assert ref == f"{now.year}-0001"  # Year changed from 2025, reset


def test_per_doc_type_independent_sequences():
    c = _company()
    now = datetime.now(timezone.utc)
    yymm = f"{now.year % 100:02d}{now.month:02d}"
    assert next_doc_ref(c, "invoice", ()) == f"INV-{yymm}-0001"
    assert next_doc_ref(c, "purchase_order", ()) == f"PO-{yymm}-0001"
    assert next_doc_ref(c, "invoice", ()) == f"INV-{yymm}-0002"
    assert next_doc_ref(c, "purchase_order", ()) == f"PO-{yymm}-0002"


def _refusal_key(fn, *args, **kwargs) -> str:
    with pytest.raises(Refused) as refused:
        fn(*args, **kwargs)
    return refused.value.detail["message_key"]


def test_unsupported_type_is_refused_by_name():
    assert _refusal_key(next_doc_ref, _company(), "bogus", ()) == "documents.err_numbering_unknown_type"


def test_a_pattern_without_a_sequence_token_is_refused():
    assert _refusal_key(require_pattern, "{PREFIX}-{YYYY}") == "documents.err_numbering_no_sequence_token"


def test_a_pattern_with_an_unknown_token_is_refused():
    assert _refusal_key(require_pattern, "{##}-{INVALID}") == "documents.err_numbering_unknown_tokens"


def test_valid_patterns_are_accepted():
    for pattern in ("{YY}{MM}-{##}", "{PREFIX}-{YYYY}-{####}", "{DD}/{MM}/{YY}-{###}"):
        assert require_pattern(pattern) == pattern


def test_get_all_sequences_returns_all_types():
    c = _company()
    seqs = get_all_sequences(c, ())
    doc_types = {s["doc_type"] for s in seqs}
    assert "invoice" in doc_types
    assert "purchase_order" in doc_types
    assert "quotation" in doc_types
    for s in seqs:
        assert "preview" in s
        assert "pattern" in s


def test_update_sequence_changes_prefix():
    c = _company()
    update_sequence(c, "invoice", prefix="FAK")
    assert _row(c, "invoice")["prefix"] == "FAK"
    assert c.settings["sequences"]["invoice"]["prefix"] == "FAK"


def test_update_sequence_changes_pattern():
    c = _company()
    update_sequence(c, "invoice", pattern="{PREFIX}-{YYYY}-{####}")
    assert _row(c, "invoice")["pattern"] == "{PREFIX}-{YYYY}-{####}"


def test_update_sequence_rejects_invalid_pattern():
    c = _company()
    key = _refusal_key(update_sequence, c, "invoice", pattern="{PREFIX}-{YYYY}")
    assert key == "documents.err_numbering_no_sequence_token"
    assert "invoice" not in (c.settings.get("sequences") or {})


def test_update_sequence_resets_next():
    c = _company(sequences={"invoice": {"next": 50}})
    update_sequence(c, "invoice", next_num=1)
    assert _row(c, "invoice")["next"] == 1


def test_update_sequence_min_next_is_1():
    c = _company()
    update_sequence(c, "invoice", next_num=-5)
    assert _row(c, "invoice")["next"] == 1


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
    assert next_doc_ref(c, key, ()).startswith(f"{prefix}-")


@pytest.mark.parametrize("list_type", [None, "", "sale", "something_new"])
def test_an_untyped_or_unknown_list_is_a_quotation(list_type):
    assert list_sequence_key(list_type) == "quotation"


def test_existing_shared_list_counter_remains_continuous():
    c = _company(sequences={"list": {"prefix": "OLD", "pattern": "{PREFIX}-{####}", "next": 40}})
    assert list_sequence_key("quotation", c.settings) == "list"
    assert next_doc_ref(c, list_sequence_key("quotation", c.settings), ()) == "OLD-0040"
    assert c.settings["sequences"]["list"]["next"] == 41


@pytest.mark.parametrize("list_type,prefix", [("audit", "AUD"), ("writeoff", "WO"), ("shipping_doc", "SHIP")])
def test_types_that_never_drew_the_shared_counter_keep_their_own(list_type, prefix):
    """Audit, write-off and shipping Lists had their own counters before the shared one was split,
    so a company with an old shared counter still numbers them AUD-, WO- and SHIP-."""
    c = _company(sequences={"list": {"prefix": "LST", "pattern": "{PREFIX}-{####}", "next": 40}})
    assert list_sequence_key(list_type, c.settings) == list_type
    assert next_doc_ref(c, list_sequence_key(list_type, c.settings), ()).startswith(f"{prefix}-")
    assert c.settings["sequences"]["list"]["next"] == 40
    previews = {row["doc_type"]: row["preview"] for row in get_all_sequences(c, ())}
    assert previews[list_type].startswith(f"{prefix}-")


@pytest.mark.parametrize("list_type", ["quotation", "transfer", None])
def test_types_that_drew_the_shared_counter_continue_it(list_type):
    c = _company(sequences={"list": {"prefix": "LST", "pattern": "{PREFIX}-{####}", "next": 40}})
    assert next_doc_ref(c, list_sequence_key(list_type, c.settings), ()) == "LST-0040"


def test_a_transfer_counter_another_writer_made_does_not_end_the_legacy_fallback():
    """A transfer document creates the transfer counter; transfer Lists stay on the shared one
    until the transfer row is saved."""
    c = _company(sequences={"list": {"prefix": "OLD", "pattern": "{PREFIX}-{####}", "next": 40}})
    next_doc_ref(c, "transfer", ())
    assert list_sequence_key("transfer", c.settings) == "list"
    assert next_doc_ref(c, list_sequence_key("transfer", c.settings), ()) == "OLD-0040"
    update_sequence(c, "transfer")
    assert list_sequence_key("transfer", c.settings) == "transfer"
    assert list_sequence_key("quotation", c.settings) == "list"


def test_a_list_counter_a_document_starts_numbers_no_list_type():
    """A new company that issues a document of type list gets a list counter that numbers only
    those documents: quotation, untyped and transfer Lists keep their own counters."""
    c = _company()
    next_doc_ref(c, "list", ())
    assert c.settings["sequences"]["list"]["shares"] == []
    for list_type, key in (("quotation", "quotation"), (None, "quotation"), ("transfer", "transfer")):
        assert list_sequence_key(list_type, c.settings) == key
    update_sequence(c, "list", prefix="LX")
    assert list_sequence_key("quotation", c.settings) == "quotation"


def test_quotation_lists_stay_on_the_legacy_counter_after_a_quotation_document():
    """The quotation counter is the documents' counter: issuing a quotation document or
    saving the quotation row never moves quotation Lists off the shared counter."""
    c = _company(sequences={
        "list": {"prefix": "OLD", "pattern": "{PREFIX}-{####}", "next": 40},
        "quotation": {"prefix": "QUO", "pattern": "{PREFIX}-{####}", "next": 3},
    })
    assert list_sequence_key("quotation", c.settings) == "list"
    assert list_sequence_key(None, c.settings) == "list"
    update_sequence(c, "quotation", prefix="QTE")
    assert list_sequence_key("quotation", c.settings) == "list"
    assert c.settings["sequences"]["list"]["next"] == 40


def test_legacy_sequence_preview_and_type_configuration_do_not_reset_numbering():
    c = _company(sequences={
        "list": {"prefix": "OLD", "pattern": "{PREFIX}-{####}", "next": 40},
    })
    previews = {row["doc_type"]: row["preview"] for row in get_all_sequences(c, ())}
    assert previews["list"] == "OLD-0040"
    assert previews["transfer"] == "OLD-0040"
    update_sequence(c, "transfer", prefix="TRF")
    assert list_sequence_key("transfer", c.settings) == "transfer"
    assert next_doc_ref(c, "transfer", ()) == "TRF-0040"
    assert c.settings["sequences"]["list"]["next"] == 40


def test_legacy_split_preserves_old_count_but_uses_new_type_prefix():
    c = _company(sequences={"list": {
        "prefix": "LEGACY", "pattern": "{PREFIX}-{####}", "next": 41,
    }})
    update_sequence(c, "transfer")
    assert _row(c, "transfer")["preview"] == "TRF-0041"
    assert list_sequence_key("transfer", c.settings) == "transfer"
    assert next_doc_ref(c, "transfer", ()) == "TRF-0041"
    assert next_doc_ref(c, "list", ()) == "LEGACY-0041"


def test_split_refuses_same_namespace_as_legacy_counter_without_mutation():
    c = _company(sequences={"list": {
        "prefix": "TRF", "pattern": "{PREFIX}-{####}", "next": 41,
    }})
    before = dict(c.settings["sequences"])
    for kwargs in ({}, {"prefix": "TRF"}):
        with pytest.raises(Refused) as refused:
            update_sequence(c, "transfer", **kwargs)
        assert refused.value.detail["message_key"] == "documents.err_numbering_overlap"
        assert c.settings["sequences"] == before
    update_sequence(c, "transfer", prefix="XFER")
    assert _row(c, "transfer")["preview"] == "XFER-0041"


def test_distinct_list_counters_cannot_be_configured_with_same_format():
    c = _company(sequences={"quotation": {
        "prefix": "SHARED", "pattern": "{PREFIX}-{####}", "next": 6,
    }})
    with pytest.raises(Refused) as refused:
        update_sequence(c, "transfer", prefix="SHARED", pattern="{PREFIX}-{####}")
    assert refused.value.detail["message_key"] == "documents.err_numbering_overlap"
    assert "transfer" not in c.settings["sequences"]
    update_sequence(c, "transfer", prefix="TRF", pattern="{PREFIX}-{####}")
    assert _row(c, "transfer")["prefix"] == "TRF"
