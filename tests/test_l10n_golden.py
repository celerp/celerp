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
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

GOLDEN = Path(__file__).resolve().parent / "golden" / "l10n_seed_pin.json"
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


def _golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.is_file() else {}


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
    expected = _golden().get(code)
    assert expected is not None, f"no golden leaves for {code!r}"
    added = sorted(set(leaves) - set(expected))
    removed = sorted(set(expected) - set(leaves))
    changed = sorted(p for p in set(leaves) & set(expected) if leaves[p] != expected[p])
    assert not (added or removed or changed), (
        f"country {code or '(none)'}: added {added}, removed {removed}, changed {changed}")


def test_chart_rows_unchanged():
    """The default chart every company starts with is the one the release before the move seeded."""
    from celerp_accounting.routes import DEFAULT_CHART_OF_ACCOUNTS

    assert _hash(DEFAULT_CHART_OF_ACCOUNTS) == _golden()["_chart"]
