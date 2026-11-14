# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Every field of a country pack, generated: ``validate`` and ``lock`` accept a pack exactly when
an independent statement of the pack format says it is valid, never fail with an exception, and
``lock`` writes the lock only for a valid pack, with that pack's version and digest.

The statement below is written from the format, not from the schema code: a value a pack gives
must mean something where it is read, so a field it writes is never null, a name is never blank,
an unknown field is refused, and the version is a plain tag word (letters and digits, joined by
dots or hyphens) that fetch can name as ``v<version>``."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import re
import shutil
from pathlib import Path

import pytest

from celerp.routers.companies import CountryPack
from celerp.services.currencies import CURRENCY_CODES
from test_l10n_packs_script import FIXTURE, _run

SEEDS = range(8)
CASES_PER_SEED = 40

_TOP = {"format", "schema_version", "country", "version", "company_defaults", "taxes", "retired"}
_TAX = {"name", "rate", "tax_type", "is_default", "description", "is_compound", "default_order"}
_ENTRY = {"id", "match", "currency", "rows"}
_ROW = {"name", "rate", "is_default", "is_compound"}
_VERSION = re.compile(r"[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*")
_ID = re.compile(r"[a-z0-9][a-z0-9-]*")

HOSTILE = [None, "", "  ", "\t", "x", "VAT 7%", " vat 7% ", "THB", "ZZZ", "2026.1", "2026/1", "v1", "-1",
           "fixture", "celerp-localization", "TH", "MY", "all", "any", "most", "both", "sales", "purchase",
           "th-old", "TH old", 0, 1, 2, -1, 7.0, 1.5, float("nan"), float("inf"), True, False, [], {}, [None],
           {"name": "VAT 7%"}, {"currency": "THB"}, "7"]


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _named(v) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _key(name: str) -> str:
    return name.strip().casefold()


def _valid_tax(t) -> bool:
    return (isinstance(t, dict) and set(t) <= _TAX and {"name", "rate"} <= set(t) and _named(t["name"])
            and _is_number(t["rate"]) and t.get("tax_type", "both") in ("sales", "purchase", "both")
            and all(isinstance(t[k], bool) for k in ("is_default", "is_compound") if k in t)
            and isinstance(t.get("description", ""), str) and _is_int(t.get("default_order", 0)))


def _valid_row(r) -> bool:
    return (isinstance(r, dict) and set(r) <= _ROW and "name" in r and _named(r["name"])
            and ("rate" not in r or _is_number(r["rate"]))
            and all(isinstance(r[k], bool) for k in ("is_default", "is_compound") if k in r))


def _valid_entry(e) -> bool:
    return (isinstance(e, dict) and set(e) <= _ENTRY and {"id", "match", "rows"} <= set(e)
            and isinstance(e["id"], str) and _ID.fullmatch(e["id"]) is not None and e["match"] in ("all", "any")
            and ("currency" not in e or e["currency"] in CURRENCY_CODES)
            and isinstance(e["rows"], list) and len(e["rows"]) > 0 and all(_valid_row(r) for r in e["rows"]))


def reference_valid(pack, code: str) -> bool:
    if not isinstance(pack, dict) or not set(pack) <= _TOP or not (_TOP - {"retired"}) <= set(pack):
        return False
    defaults, taxes, retired = pack["company_defaults"], pack["taxes"], pack.get("retired", [])
    return (pack["format"] == "celerp-localization" and _is_int(pack["schema_version"])
            and pack["schema_version"] == 1 and pack["country"] == code
            and isinstance(pack["version"], str) and _VERSION.fullmatch(pack["version"]) is not None
            and isinstance(defaults, dict) and set(defaults) == {"currency"} and defaults["currency"] in CURRENCY_CODES
            and isinstance(taxes, list) and all(_valid_tax(t) for t in taxes)
            and len({_key(t["name"]) for t in taxes}) == len(taxes)
            and isinstance(retired, list) and all(_valid_entry(e) for e in retired)
            and len({e["id"] for e in retired}) == len(retired))


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
            assert entry == {"version": pack["version"], "schema_version": 1,
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
    assert CountryPack.model_validate(pack).model_dump(exclude_unset=True) == pack
