# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company's tax list has one shape wherever it comes from: the tax settings API and the
country packs are both checked by the TaxRatesPatch schema. A tax is for sales, purchases or
both, its default flag is a true or false value, and no two taxes of one list share a name
(ignoring case and surrounding spaces). A row stored without a side, or with a blank one, is
read as both, as every reader of the list reads it."""
from __future__ import annotations

import pytest

VALID = {"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True}

INVALID = {
    "side outside sales, purchase, both": [{**VALID, "tax_type": "output"}],
    "side spelled in capitals": [{**VALID, "tax_type": "Sales"}],
    "default flag not a true or false value": [{**VALID, "is_default": "yes"}],
    "default flag a number": [{**VALID, "is_default": 1}],
    "two taxes named alike": [VALID, {**VALID, "name": " vat 7% ", "is_default": False}],
}


@pytest.mark.parametrize("path", ["/companies/me/taxes", "/companies/me/purchasing-taxes"])
@pytest.mark.parametrize("case", list(INVALID))
async def test_tax_settings_refuse_an_invalid_list_and_keep_the_saved_one(client, auth, path, case):
    saved = [VALID, {**VALID, "name": "Exempt", "rate": 0.0, "is_default": False}]
    r = await client.patch(path, headers=auth["headers"], json={"taxes": saved})
    assert r.status_code == 200, r.text
    before = (await client.get(path, headers=auth["headers"])).json()
    r = await client.patch(path, headers=auth["headers"], json={"taxes": INVALID[case]})
    assert r.status_code == 422, r.text
    assert (await client.get(path, headers=auth["headers"])).json() == before


@pytest.mark.parametrize("stored", [None, ""])
async def test_a_row_stored_without_a_side_reads_as_both(client, auth, stored):
    r = await client.patch("/companies/me/taxes", headers=auth["headers"],
                           json={"taxes": [{**VALID, "tax_type": stored}]})
    assert r.status_code == 200, r.text
    [row] = (await client.get("/companies/me/taxes", headers=auth["headers"])).json()
    assert row["tax_type"] == "both"


def test_one_list_of_allowed_sides():
    from typing import get_args

    from celerp.routers.companies import TaxRate
    from celerp.tax_regimes import TAX_TYPES
    assert TAX_TYPES == ("sales", "purchase", "both")
    assert get_args(TaxRate.model_fields["tax_type"].annotation) == TAX_TYPES
