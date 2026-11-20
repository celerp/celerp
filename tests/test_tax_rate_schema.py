# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A company's tax list has one shape wherever it comes from: the tax settings API checks it
with the TaxRatesPatch schema and the country packs with the CountryPack schema. A tax is
for sales, purchases or both, its default flag is true or false (0, 1 or a quoted "true" is
refused naming the field, in the API as in a pack file), and no two taxes of one list
share a name (ignoring case and surrounding spaces). A row stored without a side, or with a
blank one, is read as both, as every reader of the list reads it."""
from __future__ import annotations

import pytest

from test_helpers import TAX_LIST_PATHS, save_taxes

VALID = {"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True}

INVALID = {
    "side outside sales, purchase, both": [{**VALID, "tax_type": "output"}],
    "side spelled in capitals": [{**VALID, "tax_type": "Sales"}],
    "default flag not a true or false value": [{**VALID, "is_default": "maybe"}],
    "default flag a number": [{**VALID, "is_default": 1}],
    "default flag a quoted true": [{**VALID, "is_default": "true"}],
    "two taxes named alike": [VALID, {**VALID, "name": " vat 7% ", "is_default": False}],
}


@pytest.mark.parametrize("side", list(TAX_LIST_PATHS))
@pytest.mark.parametrize("case", list(INVALID))
async def test_tax_settings_refuse_an_invalid_list_and_keep_the_saved_one(client, auth, side, case):
    path = TAX_LIST_PATHS[side]
    saved = [VALID, {**VALID, "name": "Exempt", "rate": 0.0, "is_default": False}]
    r = await save_taxes(client, auth["headers"], saved, side)
    assert r.status_code == 200, r.text
    before = (await client.get(path, headers=auth["headers"])).json()
    r = await save_taxes(client, auth["headers"], INVALID[case], side)
    assert r.status_code == 422, r.text
    assert (await client.get(path, headers=auth["headers"])).json() == before


@pytest.mark.parametrize("given", [True, False])
async def test_tax_settings_store_the_default_flag_given(client, auth, given):
    r = await save_taxes(client, auth["headers"], [{**VALID, "is_default": given}])
    assert r.status_code == 200, r.text
    [row] = (await client.get("/companies/me/taxes", headers=auth["headers"])).json()
    assert row["is_default"] is given


@pytest.mark.parametrize("stored", [None, ""])
async def test_a_row_stored_without_a_side_reads_as_both(client, auth, stored):
    r = await save_taxes(client, auth["headers"], [{**VALID, "tax_type": stored}])
    assert r.status_code == 200, r.text
    [row] = (await client.get("/companies/me/taxes", headers=auth["headers"])).json()
    assert row["tax_type"] == "both"


def test_one_list_of_allowed_sides():
    from typing import get_args

    from celerp.localization.taxcodes import TaxCode
    from celerp.tax_regimes import TAX_TYPES
    assert TAX_TYPES == ("sales", "purchase", "both")
    assert get_args(TaxCode.model_fields["tax_type"].annotation) == TAX_TYPES
