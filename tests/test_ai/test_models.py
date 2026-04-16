# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BSL-1.1

"""Tests for celerp/ai/models.py - model routing."""

import pytest
from celerp.ai.models import (
    BULK_EXTRACTION,
    CLASSIFY,
    COMPLEX,
    SINGLE_FILE,
    TEXT_QUERY,
    select_model,
)


@pytest.mark.parametrize("query,expected", [
    ("how many items do I have", TEXT_QUERY),
    ("what is my AR balance", TEXT_QUERY),
    ("list all outstanding invoices", TEXT_QUERY),
    ("show me low stock items", TEXT_QUERY),
    ("count the deals in pipeline", TEXT_QUERY),
    ("total inventory value", TEXT_QUERY),
    ("give me a quick status update", TEXT_QUERY),
    ("what are my top suppliers", TEXT_QUERY),
    ("analyse my cash flow trends and recommend actions", COMPLEX),
    ("suggest reorder quantities based on sales velocity", COMPLEX),
    ("compare Q1 vs Q2 revenue performance", COMPLEX),
])
def test_select_model_text_only(query, expected):
    assert select_model(query, file_count=0, is_batch=False) == expected


def test_select_model_single_file():
    assert select_model("what is this", file_count=1, is_batch=False) == SINGLE_FILE
    assert select_model("how many items", file_count=1, is_batch=False) == SINGLE_FILE


def test_select_model_batch():
    assert select_model("process these", file_count=5, is_batch=True) == BULK_EXTRACTION
    assert select_model("how many items", file_count=100, is_batch=True) == BULK_EXTRACTION
