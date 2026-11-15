# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A new company in every country starts exactly as it did before the country tax material
moved out of core into the country packs.

For each country code (and for no country, and for a code Celerp has no rates for) a company is
registered and named in that country over HTTP, as setup does; its currency, taxes, chart of
accounts and the setup country list are flattened to leaf paths, and each leaf is compared, as a
sha256 of its canonical JSON, with tests/golden/l10n_seed_pin.json. The file holds hashes only,
so no country data sits in core. It was written from the release before the move:

    CELERP_L10N_GOLDEN_WRITE=<path to the golden file> pytest tests/test_l10n_golden.py \
        -o addopts="" -W ignore::pytest.PytestUnknownMarkWarning -q

run in a worktree of that release with this file copied in. With the variable set, each case
writes its leaves instead of comparing. The seed comparison needs the real packs (marker
``real_packs``: ``scripts/l10n_packs.py fetch`` first).

That file is never rewritten: it is the proof that the packs as first extracted (versions in
PIN_VERSIONS) seed what core did. A later pack version whose seeds change on purpose comes with
its own expectation, tests/golden/l10n_seed_<code>_<version>.json (the leaves of that one code,
as the write mode gives them), added by the change that moves the lock to it. Each country is
compared with the file for the version its lock entry names; a version with neither fails.

The chart of accounts is core's, not a pack's: every country starts with the same rows, and core
changes them on purpose. So the ``chart`` leaves of every country are compared with
tests/golden/l10n_default_chart.json instead of the pack expectation, and that file also holds
the hash of DEFAULT_CHART_OF_ACCOUNTS. The pin's ``_chart`` entry and chart leaves stay as the
pin's release had them and are not read. A change to the default chart rewrites it in the same
commit, with the chart write mode (no real packs needed):

    CELERP_L10N_CHART_WRITE=tests/golden/l10n_default_chart.json pytest tests/test_l10n_golden.py \
        -k chart -o addopts="" -q
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN = GOLDEN_DIR / "l10n_seed_pin.json"
CHART = GOLDEN_DIR / "l10n_default_chart.json"
CHART_PREFIX = "chart["
PIN_VERSIONS = ("2026.1", "2026.2")
CODES = ("TH", "SG", "MY", "PH", "ID", "VN", "AU", "NZ", "GB", "DE", "FR", "ES", "IT", "PT", "US", "CA",
         "MX", "IN", "JP", "AE", "SA", "ET")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _leaves(value, path: str = ""):
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _leaves(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list) and value:
        for i, item in enumerate(value):
            yield from _leaves(item, f"{path}[{i}]")
    else:
        yield path, value


def _golden(golden_dir: Path = GOLDEN_DIR) -> dict:
    path = golden_dir / GOLDEN.name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _pack_leaves(code: str, lock: dict, golden_dir: Path) -> dict:
    pin = _golden(golden_dir)
    version = lock[code]["version"] if code in lock else None
    if version is None or version in PIN_VERSIONS:
        assert code in pin, f"no golden leaves for {code!r}"
        return pin[code]
    path = golden_dir / f"l10n_seed_{code}_{version}.json"
    assert path.is_file(), f"no expected seeds for {code} at pack version {version}: add tests/golden/{path.name}"
    return json.loads(path.read_text(encoding="utf-8"))


def expected_leaves(code: str, lock: dict, golden_dir: Path = GOLDEN_DIR) -> dict:
    """The leaves a new company in ``code`` must have: its pack's leaves for the version ``lock``
    names, with the chart leaves taken from core's chart expectation."""
    pack = {p: h for p, h in _pack_leaves(code, lock, golden_dir).items() if not p.startswith(CHART_PREFIX)}
    return pack | json.loads((golden_dir / CHART.name).read_text(encoding="utf-8"))["leaves"]


def _write(target: str, key: str, value) -> None:
    path = Path(target)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    data[key] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")


async def _snapshot(client, code: str) -> dict:
    from ui.routes.settings import location_address
    from ui.routes.setup import tax_country_options

    r = await client.post("/auth/register", json={
        "company_name": f"Golden {code or 'none'}", "email": f"golden-{uuid.uuid4().hex[:8]}@example.com",
        "name": "Owner", "password": "pwvalid1"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['access_token']}"}
    if code:
        items = (await client.get("/companies/me/locations", headers=h)).json()["items"]
        default = next((loc for loc in items if loc.get("is_default")), None)
        if default is None:
            r = await client.post("/companies/me/locations", headers=h,
                                  json={"name": "Head office", "type": "office", "is_default": True})
            assert r.status_code == 200, r.text
            default = r.json()
        r = await client.patch(f"/companies/me/locations/{default['id']}", headers=h,
                               json={"address": location_address(default.get("address"), country=code)})
        assert r.status_code == 200, r.text
    settings = (await client.get("/companies/me", headers=h)).json()["settings"]
    chart = (await client.get("/accounting/chart", headers=h)).json()["items"]
    return {
        "currency": settings.get("currency"),
        "taxes": settings.get("taxes"),
        "taxes_seeded_for": settings.get("taxes_seeded_for"),
        "chart": [[a["code"], a["name"], a["account_type"], a["parent_code"]] for a in chart],
        "setup_options": [[code_, str(name)] for code_, name in tax_country_options()],
    }


@pytest.mark.real_packs
@pytest.mark.parametrize("code", CODES + ("", "XX"))
async def test_l10n_seed_matches_pin_golden(client, code):
    leaves = {path: _hash(value) for path, value in _leaves(await _snapshot(client, code))}
    target = os.environ.get("CELERP_L10N_GOLDEN_WRITE")
    if target:
        _write(target, code, leaves)
        return
    from celerp.localization import _lock

    expected = expected_leaves(code, _lock() or {})
    added = sorted(set(leaves) - set(expected))
    removed = sorted(set(expected) - set(leaves))
    changed = sorted(p for p in set(leaves) & set(expected) if leaves[p] != expected[p])
    assert not (added or removed or changed), (
        f"country {code or '(none)'}: added {added}, removed {removed}, changed {changed}")


async def test_chart_rows_unchanged(client):
    """The default chart, and the chart a new company starts with, are the ones the chart
    expectation records."""
    from celerp_accounting.routes import DEFAULT_CHART_OF_ACCOUNTS

    chart = {"_chart": _hash(DEFAULT_CHART_OF_ACCOUNTS),
             "leaves": {p: _hash(v) for p, v in _leaves((await _snapshot(client, ""))["chart"], "chart")}}
    target = os.environ.get("CELERP_L10N_CHART_WRITE")
    if target:
        Path(target).write_text(json.dumps(chart, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return
    assert chart == json.loads(CHART.read_text(encoding="utf-8"))


def _golden_dir(tmp_path: Path) -> Path:
    (tmp_path / GOLDEN.name).write_text(json.dumps({
        "TH": {"taxes[0].rate": "pin", "chart[0][0]": "old core"}, "": {"currency": "none"}}), encoding="utf-8")
    (tmp_path / "l10n_seed_TH_2027.1.json").write_text(
        json.dumps({"taxes[0].rate": "later", "chart[0][0]": "old core"}), encoding="utf-8")
    (tmp_path / CHART.name).write_text(json.dumps({"_chart": "core", "leaves": {"chart[0][0]": "core"}}),
                                       encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("version", PIN_VERSIONS)
def test_a_pinned_pack_version_is_compared_with_the_pin(tmp_path, version):
    golden = _golden_dir(tmp_path)
    assert expected_leaves("TH", {"TH": {"version": version}}, golden) == {
        "taxes[0].rate": "pin", "chart[0][0]": "core"}
    assert expected_leaves("", {"TH": {"version": version}}, golden) == {"currency": "none", "chart[0][0]": "core"}


def test_a_later_pack_version_is_compared_with_its_own_expectation(tmp_path):
    golden = _golden_dir(tmp_path)
    assert expected_leaves("TH", {"TH": {"version": "2027.1"}}, golden) == {
        "taxes[0].rate": "later", "chart[0][0]": "core"}
    pin = (golden / GOLDEN.name).read_bytes()
    with pytest.raises(AssertionError, match=r"TH at pack version 2027\.2: add tests/golden/l10n_seed_TH_2027\.2\.json"):
        expected_leaves("TH", {"TH": {"version": "2027.2"}}, golden)
    assert (golden / GOLDEN.name).read_bytes() == pin
