# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Country packs at schema 2: the cross-field rules ``validate`` holds every pack to, the seed
fixes the locked packs carry, the install-country list every listed country must have a pack
for, and the guard that keeps country branches out of core.
"""
from __future__ import annotations

import copy
import http.server
import importlib.util
import json
import os
import threading
from datetime import date
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"{name}_under_test", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_pack() -> dict:
    """A schema 2 pack that uses every rule ``validate`` checks, each the right way."""
    return {
        "format": "celerp-localization",
        "schema_version": 2,
        "country": "TH",
        "version": "fixture",
        "company_defaults": {"currency": "THB"},
        "taxes": [
            {"id": "th-vat-7", "name": "VAT 7%", "tax_type": "both", "category": "standard", "is_default": True,
             "versions": [{"rate": 7.0, "valid_to": "2026-12-31"}, {"rate": 8.0, "valid_from": "2027-01-01"}]},
            {"id": "th-wht-3", "name": "WHT 3%", "tax_type": "purchase", "category": "withholding",
             "withholding_point": "payment", "input_account": "1150", "versions": [{"rate": 3.0}]},
            {"id": "th-vat-7-cash", "name": "VAT 7% cash", "tax_type": "sales", "category": "standard",
             "cash_basis": True, "versions": [{"rate": 7.0}]},
            {"id": "th-exempt", "name": "Exempt", "tax_type": "both", "category": "exempt",
             "versions": [{"rate": 0.0}]},
        ],
        "cash_basis_cutoff": {"month": 12, "day": 31, "years_after": 1},
        "item_rate_table": [
            {"hs_prefix": "8471", "code_id": "th-vat-7", "valid_from": "2026-01-01"},
            {"hs_prefix": "8471", "code_id": "th-exempt", "valid_from": "2027-01-01"},
            {"hs_prefix": "847130", "code_id": "th-exempt", "valid_from": "2026-01-01"},
        ],
        "retired": [{"id": "th-wht-2-2025", "match": "any",
                     "rows": [{"name": "WHT 2%", "rate": 2.0, "category": "withholding"}]}],
    }


def defects(pack: dict, tmp_path: Path) -> list[str]:
    """What ``validate`` says about ``pack`` written as its country's file."""
    script = _load("l10n_packs")
    root = tmp_path / "l10n"
    (root / "packs").mkdir(parents=True, exist_ok=True)
    (root / "packs" / f"{pack['country']}.json").write_bytes(script.normalized(pack))
    return script.validate(root, against_lock=False)


def _tax(i: int, **fields):
    def change(pack):
        pack["taxes"][i].update(fields)
    return change


def _drop_tax_field(i: int, field: str):
    def change(pack):
        del pack["taxes"][i][field]
    return change


def _overlap(pack):
    pack["taxes"][0]["versions"] = [{"rate": 7.0, "valid_to": "2026-12-31"}, {"rate": 8.0, "valid_from": "2026-06-01"}]


def _open_start_later(pack):
    pack["taxes"][0]["versions"] = [{"rate": 7.0, "valid_to": "2026-12-31"}, {"rate": 8.0}]


def _no_cutoff(pack):
    del pack["cash_basis_cutoff"]


def _rate_row(i: int, **fields):
    def change(pack):
        pack["item_rate_table"][i].update(fields)
    return change


def _duplicate_rate_row(pack):
    pack["item_rate_table"].append(dict(pack["item_rate_table"][0]))


def _negative_retired(pack):
    pack["retired"][0]["rows"][0]["rate"] = -2.0


_REFUSED = {
    # V17: each names the field it refuses.
    "schema 1": (lambda p: p.__setitem__("schema_version", 1), "schema_version"),
    "missing category": (_drop_tax_field(0, "category"), "category"),
    "category outside the list": (_tax(0, category="luxury"), "category"),
    "negative rate": (_tax(3, versions=[{"rate": -3.0}]), "rate"),
    "bare rate beside versions": (_tax(3, rate=0.0), "rate"),
    "no version": (_tax(3, versions=[]), "versions"),
    "overlapping versions": (_overlap, "versions"),
    "open start on a later version": (_open_start_later, "valid_from"),
    "withholding without point": (_drop_tax_field(1, "withholding_point"), "withholding_point"),
    "point outside document and payment": (_tax(1, withholding_point="invoice"), "withholding_point"),
    "cash basis without cutoff": (_no_cutoff, "cash_basis_cutoff"),
    "account outside the default chart": (_tax(1, input_account="9999"), "input_account"),
    "negative retired rate": (_negative_retired, "retired"),
    # V28: each names the row.
    "prefix of one digit": (_rate_row(0, hs_prefix="8"), "item_rate_table"),
    "prefix of nine digits": (_rate_row(0, hs_prefix="847130000"), "item_rate_table"),
    "prefix not digits": (_rate_row(0, hs_prefix="84A1"), "item_rate_table"),
    "unknown code id": (_rate_row(0, code_id="th-nope"), "item_rate_table"),
    "duplicate prefix and date": (_duplicate_rate_row, "item_rate_table"),
}


def test_pack_schema2_cross_field_rules(tmp_path):
    """``validate`` takes a schema 2 pack and refuses schema 1 and each V17 and V28 case, naming
    the field or row it refuses."""
    assert defects(valid_pack(), tmp_path) == []
    for case, (change, named) in _REFUSED.items():
        pack = copy.deepcopy(valid_pack())
        change(pack)
        found = defects(pack, tmp_path / case.replace(" ", "-"))
        assert found, f"{case}: accepted"
        assert any(named in d for d in found), f"{case}: {found}"


def _real_root() -> Path:
    from celerp.localization import _root
    return _root()


def test_pack_lock_is_schema2():
    """Every locked pack, real and fixture, is cut at schema 2: no reader for schema 1 is left."""
    for lock in (REPO / "celerp" / "localization" / "packs.lock.json", REPO / "tests" / "fixtures" / "l10n" / "packs.lock.json"):
        entries = json.loads(lock.read_text(encoding="utf-8"))
        assert {code: e["schema_version"] for code, e in entries.items() if e["schema_version"] != 2} == {}, lock


@pytest.mark.real_packs
def test_baseline_packs_schema2_seed_fixes():
    """Every locked pack validates at schema 2, and VN, ID and ET carry their seed fixes."""
    from celerp.localization.taxcodes import TaxCode, rate_on

    root = _real_root()
    assert _load("l10n_packs").validate(root) == []
    taxes = {code: {t["name"]: TaxCode.model_validate(t) for t in json.loads(
        (root / "packs" / f"{code}.json").read_text(encoding="utf-8"))["taxes"]} for code in ("VN", "ID", "ET")}

    vat8 = taxes["VN"]["VAT 8%"]
    assert rate_on(vat8, date(2026, 12, 31)) == 8.0
    assert rate_on(vat8, date(2027, 1, 1)) == 10.0

    ppn = taxes["ID"]["PPN 11%"]
    (version,) = ppn.versions
    assert version.rate == 12.0 and version.base_fraction == pytest.approx(11 / 12)

    wht = taxes["ET"]["WHT 3%"]
    assert (wht.category, wht.withholding_point) == ("withholding", "document")
    assert [v.rate for v in wht.versions] == [3.0]


def _install_list() -> Path:
    return REPO / "celerp" / "localization" / "install_countries.json"


def test_check_install_countries_have_packs(tmp_path):
    """Every country with installs has a locked pack, PT excepted; a listed country without one
    fails the check by name."""
    check = _load("check_install_countries_have_packs")
    assert check.main([]) == 0
    listed = json.loads(_install_list().read_text(encoding="utf-8"))
    assert listed and all(len(code) == 2 and code.isupper() and isinstance(n, int) for code, n in listed.items())

    planted = tmp_path / "install_countries.json"
    planted.write_text(json.dumps({**listed, "ZZ": 1, "PT": 3}), encoding="utf-8")
    assert check.missing(planted) == ["ZZ"]
    assert check.main([str(planted)]) != 0


class _Relay(http.server.BaseHTTPRequestHandler):
    status = 200
    body = b'{"TH": 4, "AR": 2}'

    def do_GET(self):  # noqa: N802 - the http.server hook name
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def relay():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Relay)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    _Relay.status, _Relay.body = 200, b'{"TH": 4, "AR": 2}'


def test_install_countries_script_keeps_file_on_error(tmp_path, relay, monkeypatch):
    """The build step writes the counts the relay reports; a relay error, a body that is not
    counts, or a failing write exits non-zero and leaves the file as it was."""
    script = _load("install_countries")
    out = tmp_path / "install_countries.json"
    url = f"http://127.0.0.1:{relay.server_port}/install-countries"
    assert script.main(["--url", url, "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"AR": 2, "TH": 4}
    before = out.read_bytes()

    for status, body in ((500, b"{}"), (200, b"not json"), (200, b'{"TH": "four"}'), (200, b'{"Thailand": 4}')):
        _Relay.status, _Relay.body = status, body
        assert script.main(["--url", url, "--out", str(out)]) != 0, body
        assert out.read_bytes() == before

    _Relay.status, _Relay.body = 200, b'{"TH": 5}'

    def failing_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", failing_replace)
    assert script.main(["--url", url, "--out", str(out)]) != 0
    assert out.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["install_countries.json"]


def test_check_core_has_no_country_branch(tmp_path):
    """No country literal drives a condition in core or the documents module; a planted
    ``if country == "PH"`` fails the check by file and line."""
    check = _load("check_core_has_no_country_branch")
    assert check.main([str(REPO)]) == 0

    planted = tmp_path / "celerp" / "services"
    planted.mkdir(parents=True)
    (planted / "planted.py").write_text(
        "def rule(country):\n    if country == \"PH\":\n        return 1\n    return 0\n", encoding="utf-8")
    found = check.branches(tmp_path)
    assert [(Path(path).name, line) for path, line in found] == [("planted.py", 2)]
    assert check.main([str(tmp_path)]) != 0

    (planted / "planted.py").write_text("CODE = \"PH\"\n", encoding="utf-8")
    assert check.branches(tmp_path) == []
