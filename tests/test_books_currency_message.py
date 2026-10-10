# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A payment taken while the company kept its books in another currency is never posted
on the books it keeps now: posting it would mix two currencies. The refusal says why
and what to do instead, a correcting entry in Accounting. Both writers share the check:
recording an online payment and applying a refund of one."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from celerp_docs import routes, routes_payments

pytestmark = pytest.mark.asyncio


class _Session:
    def __init__(self, settings):
        self._company = type("C", (), {"settings": settings})()

    async def execute(self, _stmt):
        company = self._company
        return type("R", (), {"scalar_one_or_none": lambda self: company})()


async def test_refusal_says_why_and_points_to_a_correcting_entry():
    with pytest.raises(HTTPException) as e:
        await routes.books_currency_still(_Session({"currency": "EUR"}), "c", "USD")
    assert e.value.status_code == 422
    detail = e.value.detail
    assert "USD" in detail and "EUR" in detail
    assert "mix two currencies" in detail
    assert "correcting journal entry in Accounting, Journal" in detail


async def test_both_writers_use_the_one_check():
    import inspect
    assert "books_currency_still(" in inspect.getsource(routes._books_still_kept)
    assert "books_currency_still(" in inspect.getsource(routes_payments)
