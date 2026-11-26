# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Imported tax codes say what they are. Each source system's tax types map to a category
through one shared table; a type with no mapping is refused by name while the rest of the
batch imports. Importing the same code again is not a conflict, a different rate is, and the
side a code applies to comes from the source, never from the sign of its rate."""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from celerp.importers.results import message_text
from celerp.importers.schema import CIFTaxCode
from celerp.models.company import Company
from celerp.services.migration_core_sink import _apply_tax_code
from celerp.tax_regimes import purchasing_taxes, sales_taxes
from fixtures.manager_io import specs
from fixtures.manager_io.support import adapter as manager_adapter, artifact
from test_export_set_adapters import FULL, _adapter, _qbo, _qbo_adapter, _set, _xero


def _cif(name: str, rate, **fields) -> CIFTaxCode:
    return CIFTaxCode(source_system="xero", source_type="TaxRate", source_external_id=f"tax:{name}",
                      name=name, rate_percent=D(str(rate)), **fields)


def _company() -> Company:
    return Company(settings={"taxes": [{
        "id": "u-0123456789ab", "name": "VAT 7%", "tax_type": "both", "category": "standard",
        "versions": [{"rate": 7.0}], "is_default": True, "is_compound": False, "description": ""}]})


def _all_taxes(company: Company) -> list[dict]:
    return sales_taxes(company.settings) + purchasing_taxes(company.settings)


def test_importer_reimport_same_code_is_no_conflict():
    company = _company()
    before = _all_taxes(company)
    assert _apply_tax_code(company, _cif("VAT 7%", 7, category="standard")) is None
    assert _all_taxes(company) == before

    error = _apply_tax_code(company, _cif("vat 7%", 9, category="standard"))
    assert error and "vat 7%" in message_text(error).lower() and "rate" in message_text(error)
    assert _all_taxes(company) == before

    assert _apply_tax_code(company, _cif("WHT 3%", 3, category="withholding", tax_type="purchase")) is None
    [wht] = [row for row in sales_taxes(company.settings) if row["name"] == "WHT 3%"]
    assert wht["tax_type"] == "purchase" and wht["category"] == "withholding"
    assert "rate" not in wht and wht["versions"][0]["rate"] == 3.0

    with pytest.raises(ValidationError):
        _cif("Withheld", -3, category="withholding")


def _kinds(codes) -> dict[str, str | None]:
    return {code.name: code.category for code in codes}


def test_importer_maps_tax_types_to_categories(tmp_path):
    from celerp.importers.tax_types import tax_kind

    (tmp_path / "x").mkdir()
    (tmp_path / "q").mkdir()
    assert tax_kind("xero", "Tourism Levy") is None
    xero = _adapter().build_manifest(_set(tmp_path / "x", _xero()), FULL).bundle.tax_codes
    assert _kinds(xero) == {"20% (VAT on Income)": "standard", "20% (VAT on Expenses)": "standard",
                            "20% (VAT on Capital Purchases)": "standard", "Zero Rated Income": "zero",
                            "No VAT": "out_of_scope"}
    sides = {code.name: code.tax_type for code in xero}
    assert sides["20% (VAT on Income)"] == "sales" and sides["20% (VAT on Expenses)"] == "purchase"

    qbo = _qbo_adapter().build_manifest(_set(tmp_path / "q", _qbo()), FULL).bundle.tax_codes
    assert _kinds(qbo) == {"20.0% S": "standard", "0.0% Z": "zero", "No VAT": "out_of_scope"}

    rc = specs.obj("TaxCode", "RC", {1: "Reverse charge 10%", 5: specs.CUSTOM_RATE, 6: specs.SINGLE_RATE,
                                     4: D("10"), 11: True})
    book = specs.write_manager_file(tmp_path / "rc.manager", [*specs.basic_objects(), rc], specs.basic_blobs())
    manager = manager_adapter().build_manifest([artifact(book)], FULL).bundle.tax_codes
    assert _kinds(manager)["Reverse charge 10%"] == "reverse_charge"
    assert _kinds(manager)["VAT 10%"] == "standard"
    # A total-rate code still stops the scan: its rate is the sum of other codes, not its own.
    total = specs.obj("TaxCode", "TOT", {1: "Total 12%", 5: 1, 6: specs.SINGLE_RATE, 4: D("12")})
    book = specs.write_manager_file(tmp_path / "total.manager", [*specs.basic_objects(), total], specs.basic_blobs())
    coverage = {r.source_type: r for r in manager_adapter().inspect([artifact(book)]).coverage}
    assert coverage["TaxCode (unsupported feature)"].count == 1

    company = _company()
    errors = [_apply_tax_code(company, code) for code in (
        _cif("Levy 2%", 2, category=None, source_tax_type="Tourism Levy"),
        _cif("Zero", 0, category="zero", tax_type="sales"))]
    assert errors[0] and "Levy 2%" in message_text(errors[0]) and "Tourism Levy" in message_text(errors[0])
    assert errors[1] is None
    names = {row["name"] for row in _all_taxes(company)}
    assert "Zero" in names and "Levy 2%" not in names
