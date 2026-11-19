# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every field of a country pack, generated: ``validate`` and ``lock`` accept a pack exactly when
an independent statement of the pack format says it is valid, never fail with an exception, and
``lock`` writes the lock only for a valid pack, with that pack's version and digest.

The statement below is written from the format, not from the schema code: a value a pack gives
must mean something where it is read, so a field it writes is never null, a name is never blank,
an unknown field is refused, a rate is never negative (a withheld tax says so by its category),
and the version is a plain tag word (letters and digits, joined by
dots or hyphens) that fetch can name as ``v<version>``."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import shutil
from datetime import date
from pathlib import Path

import pytest

from celerp.routers.companies import CountryPack
from celerp.services.currencies import CURRENCY_CODES
from test_l10n_packs_script import FIXTURE, _run

SEEDS = range(8)
CASES_PER_SEED = 40

_TOP = {"format", "schema_version", "country", "version", "company_defaults", "taxes", "retired", "item_rate_table",
        "cash_basis_cutoff", "identity_schemes", "subdivisions", "subdivision_rules", "fields", "legal"}
_REQUIRED = {"format", "schema_version", "country", "version", "company_defaults", "taxes"}
_TAX = {"id", "name", "tax_type", "category", "component", "output_account", "input_account", "report_tags",
        "cash_basis", "withholding_point", "versions", "is_default", "is_compound", "default_order", "description"}
_RATE_VERSION = {"rate", "valid_from", "valid_to", "base_fraction"}
_CATEGORIES = ("standard", "reduced", "zero", "exempt", "out_of_scope", "reverse_charge", "self_assessed_import",
               "withholding", "perception")
_ENTRY = {"id", "match", "currency", "rows"}
_ROW = {"name", "rate", "category", "is_default", "is_compound"}
_VERSION = re.compile(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*")
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
HOSTILE = [None, "", "  ", "\t", "x", "VAT 7%", " vat 7% ", "THB", "ZZZ", "2026.1", "2026/1", "v1", "-1",
           "fixture", "celerp-localization", "TH", "MY", "all", "any", "most", "both", "sales", "purchase",
           "th-old", "TH old", 0, 1, 2, -1, 7.0, 1.5, float("nan"), float("inf"), True, False, [], {}, [None],
           {"name": "VAT 7%"}, {"currency": "THB"}, "7", "2026-01-01", "2026-02-30", "standard", "withholding",
           "document", "payment", {"rate": 7.0}, [{"rate": 7.0}]]


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _named(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _one_of(v, allowed) -> bool:
    return isinstance(v, str) and v in allowed


def _key(name: str) -> str:
    return name.strip().casefold()


def _day(v):
    """The day ``v`` writes, else None."""
    if not (isinstance(v, str) and _DAY.fullmatch(v)):
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        return None


def _valid_versions(versions) -> bool:
    if not (isinstance(versions, list) and versions and all(
            isinstance(v, dict) and set(v) <= _RATE_VERSION and "rate" in v for v in versions)):
        return False
    spans = []
    for i, v in enumerate(versions):
        start, end = (_day(v[k]) if k in v else None for k in ("valid_from", "valid_to"))
        if any(k in v and _day(v[k]) is None for k in ("valid_from", "valid_to")):
            return False
        if not (_is_number(v["rate"]) and v["rate"] >= 0) or (i and start is None) or (start and end and end < start):
            return False
        if "base_fraction" in v and not (_is_number(v["base_fraction"]) and 0 < v["base_fraction"] <= 1):
            return False
        spans.append((start, end))
    return all(end is not None and end < start for (_, end), (start, _) in zip(spans, spans[1:]))


def _valid_tax(t) -> bool:
    return (isinstance(t, dict) and set(t) <= _TAX and {"id", "name", "category", "versions"} <= set(t)
            and isinstance(t["id"], str) and _ID.fullmatch(t["id"]) is not None and _named(t["name"])
            and t["category"] in _CATEGORIES and _valid_versions(t["versions"])
            and t.get("tax_type", "both") in ("sales", "purchase", "both")
            and t.get("withholding_point", "document") in ("document", "payment")
            and (t["category"] != "withholding" or "withholding_point" in t)
            and all(isinstance(t[k], bool) for k in ("is_default", "is_compound", "cash_basis") if k in t)
            and all(isinstance(t[k], str) for k in ("description", "component") if k in t)
            and isinstance(t.get("report_tags", []), list) and all(isinstance(x, str) for x in t.get("report_tags", []))
            and _is_int(t.get("default_order", 0)))


def _valid_row(r) -> bool:
    return (isinstance(r, dict) and set(r) <= _ROW and "name" in r and _named(r["name"])
            and ("rate" not in r or (_is_number(r["rate"]) and r["rate"] >= 0))
            and r.get("category", "standard") in _CATEGORIES
            and all(isinstance(r[k], bool) for k in ("is_default", "is_compound") if k in r))


def _valid_entry(e) -> bool:
    return (isinstance(e, dict) and set(e) <= _ENTRY and {"id", "match", "rows"} <= set(e)
            and isinstance(e["id"], str) and _ID.fullmatch(e["id"]) is not None and e["match"] in ("all", "any")
            and ("currency" not in e or _one_of(e["currency"], CURRENCY_CODES))
            and isinstance(e["rows"], list) and len(e["rows"]) > 0 and all(_valid_row(r) for r in e["rows"]))


def _valid_cutoff(c) -> bool:
    return (isinstance(c, dict) and set(c) == {"month", "day", "years_after"} and all(_is_int(v) for v in c.values())
            and 1 <= c["month"] <= 12 and 1 <= c["day"] <= 31 and c["years_after"] >= 0)


def _valid_item_rates(rows, ids) -> bool:
    if not (isinstance(rows, list) and all(
            isinstance(r, dict) and set(r) == {"hs_prefix", "code_id", "valid_from"} and isinstance(r["hs_prefix"], str)
            and re.fullmatch(r"[0-9]{2,8}", r["hs_prefix"]) and _one_of(r["code_id"], ids) and _day(r["valid_from"])
            for r in rows)):
        return False
    return len({(r["hs_prefix"], r["valid_from"]) for r in rows}) == len(rows)


def reference_valid(pack, code: str) -> bool:
    if not isinstance(pack, dict) or not set(pack) <= _TOP or not _REQUIRED <= set(pack):
        return False
    defaults, taxes, retired = pack["company_defaults"], pack["taxes"], pack.get("retired", [])
    if not (isinstance(taxes, list) and all(_valid_tax(t) for t in taxes)):
        return False
    ids = [t["id"] for t in taxes]
    cutoff = pack.get("cash_basis_cutoff")
    return (pack["format"] == "celerp-localization" and _is_int(pack["schema_version"])
            and pack["schema_version"] == 2 and pack["country"] == code
            and isinstance(pack["version"], str) and _VERSION.fullmatch(pack["version"]) is not None
            and isinstance(defaults, dict) and set(defaults) == {"currency"} and _one_of(defaults["currency"], CURRENCY_CODES)
            and len({_key(t["name"]) for t in taxes}) == len(taxes) and len(set(ids)) == len(ids)
            and isinstance(retired, list) and all(_valid_entry(e) for e in retired)
            and len({e["id"] for e in retired}) == len(retired)
            and ("cash_basis_cutoff" not in pack or _valid_cutoff(cutoff))
            and (cutoff is not None or not any(t.get("cash_basis") for t in taxes))
            and _valid_item_rates(pack.get("item_rate_table", []), set(ids)))


def _paths(node, path=()):
    """Every place in the pack a value sits, containers included."""
    yield path
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _paths(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _paths(v, (*path, i))


def _parent(pack, path):
    node = pack
    for step in path[:-1]:
        node = node[step]
    return node


def _mutate(rng: random.Random, pack: dict) -> None:
    path = rng.choice([p for p in _paths(pack) if p])
    parent, last = _parent(pack, path), path[-1]
    move = rng.random()
    if move < 0.6:
        parent[last] = copy.deepcopy(rng.choice(HOSTILE))
    elif move < 0.75 and isinstance(parent, dict):
        del parent[last]
    elif move < 0.85 and isinstance(parent, dict):
        parent[rng.choice(["note", "Name", "rate", "currency", "extra"])] = copy.deepcopy(rng.choice(HOSTILE))
    elif isinstance(parent, list):
        parent.append(copy.deepcopy(rng.choice(parent)))
    else:
        parent[last] = copy.deepcopy(rng.choice(HOSTILE))


def _base() -> dict:
    pack = json.loads((FIXTURE / "packs" / "TH.json").read_text(encoding="utf-8"))
    pack["retired"] = [{"id": "th-vat-7-2099", "match": "any", "rows": [{"name": "VAT 7%", "rate": 7.0}]},
                       {"id": "th-old", "match": "all", "currency": "THB",
                        "rows": [{"name": "Exempt", "is_default": False, "is_compound": False}]}]
    pack["taxes"][0]["versions"] = [{"rate": 7.0, "valid_to": "2026-12-31"}, {"rate": 8.0, "valid_from": "2027-01-01"}]
    pack["taxes"][1]["cash_basis"] = True
    pack["cash_basis_cutoff"] = {"month": 12, "day": 31, "years_after": 1}
    pack["item_rate_table"] = [{"hs_prefix": "8471", "code_id": pack["taxes"][0]["id"], "valid_from": "2026-01-01"}]
    return pack


def _write(root: Path, pack) -> bytes:
    data = (json.dumps(pack, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (root / "packs" / "TH.json").write_bytes(data)
    lock_path = root / "packs.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    version = pack.get("version") if isinstance(pack, dict) else None
    lock["TH"] = {**lock["TH"], "sha256": hashlib.sha256(data).hexdigest(),
                  **({"version": version} if isinstance(version, str) else {})}
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return data


def test_the_base_pack_is_valid():
    assert reference_valid(_base(), "TH")


@pytest.mark.parametrize("seed", SEEDS)
def test_validate_and_lock_accept_a_pack_exactly_when_the_format_says_it_is_valid(tmp_path, seed):
    rng = random.Random(20261115 + seed)
    root = tmp_path / "l10n"
    shutil.copytree(FIXTURE, root)
    seen = set()
    for case in range(CASES_PER_SEED):
        pack = _base()
        for _ in range(rng.choice([1, 1, 2])):
            _mutate(rng, pack)
        valid = reference_valid(pack, "TH")
        seen.add(valid)
        data = _write(root, pack)
        where = f"seed {seed} case {case}: {json.dumps(pack, allow_nan=True)[:400]}"
        code, out = _run("validate", str(root))
        assert (code == 0) == valid, f"validate {code} {out!r} for {where}"
        before = (root / "packs.lock.json").read_bytes()
        code, out = _run("lock", str(root))
        assert (code == 0) == valid, f"lock {code} {out!r} for {where}"
        if valid:
            entry = json.loads((root / "packs.lock.json").read_text(encoding="utf-8"))["TH"]
            assert entry == {"version": pack["version"], "schema_version": 2,
                             "sha256": hashlib.sha256(data).hexdigest()}, where
        else:
            assert (root / "packs.lock.json").read_bytes() == before, where
    assert seen == {True, False}, "every seed meets both valid and invalid packs"


@pytest.mark.parametrize("value", [None, "  "], ids=["null", "blank"])
def test_a_null_or_blank_value_in_any_field_is_judged_as_the_format_says(tmp_path, value):
    """The random cases reach a given field only now and then; this writes the value into every
    field of the pack in turn, the optional ones (a retired row's match fields, a retired entry's
    currency) included."""
    root = tmp_path / "l10n"
    shutil.copytree(FIXTURE, root)
    paths = [p for p in _paths(_base()) if p]
    for path in paths:
        pack = _base()
        _parent(pack, path)[path[-1]] = value
        _write(root, pack)
        code, out = _run("validate", str(root))
        assert (code == 0) == reference_valid(pack, "TH"), f"validate {code} {out!r} with {path} = {value!r}"
    assert len(paths) > 40


def test_a_valid_pack_reads_back_exactly_as_written():
    """The schema checks a pack without changing a value it gives."""
    pack = _base()
    assert CountryPack.model_validate(pack).model_dump(mode="json", exclude_unset=True) == pack
