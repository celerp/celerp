"""The help bundle validator (celerp/help/bundle.py): Table V, the shared text-entry
limits, and the site's check_bundle.py run only when a site checkout is named."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import types
from pathlib import Path

import pytest

from celerp.help import bundle as B

WEBP = b"RIFF" + (4).to_bytes(4, "little") + b"WEBP"
SHA = hashlib.sha256(WEBP).hexdigest()


def _text(title="Add an item", steps=("Click New.",), image=None):
    return {"title": title, "summary": "Create a stock item.",
            "steps": [{"text": s, "image": image} for s in steps], "updated": "2026-11-01"}


def _bundle():
    return {
        "schema": 1, "version": "2.10.0", "commit": "a" * 40,
        "site_languages": ["en", "th"], "locales": ["en", "th"],
        "modules": [{"name": "core", "title": "Celerp"}, {"name": "docs", "title": "Documents"}],
        "topics": [
            {"id": "core.settings", "module": "core", "order": 1, "related": ["docs.memos"],
             "text": {"en": _text("Settings", image=SHA), "th": _text("ตั้งค่า")}},
            {"id": "docs.memos", "module": "docs", "order": 1, "related": [],
             "text": {"en": _text("Memos")}},
        ],
        "images": {SHA: base64.b64encode(WEBP).decode()},
        "tombstones": [{"id": "docs.old", "kind": "topic", "replaced_by": "docs.memos"},
                       {"id": "gone", "kind": "module", "replaced_by": ""}],
    }


@pytest.fixture(autouse=True)
def _no_site(monkeypatch):
    monkeypatch.delenv("CELERP_SITE", raising=False)


def test_a_valid_bundle_has_no_problem():
    assert B.bundle_problems(_bundle()) == []


def _set(path, value):
    def apply(b):
        *parents, last = path
        node = b
        for key in parents:
            node = node[key]
        node[last] = value
    return apply


def _dup_topic(b):
    b["topics"].append(copy.deepcopy(b["topics"][1]))


def _title_clash(b):
    b["topics"][1]["text"]["en"]["title"] = "settings"


CASES = {
    "V1": _set(["schema"], 2),
    "V2": _set(["version"], "2.10"),
    "V3": _set(["locales"], ["th"]),
    "V4": lambda b: b["modules"].append({"name": "empty", "title": "Empty"}),
    "V5": _dup_topic,
    "V6": _set(["topics", 1, "text", "en", "steps"], [{"text": "x", "image": None}] * 21),
    "V7": _title_clash,
    "V8": _set(["topics", 1, "related"], ["nope.page"]),
    "V9": _set(["images", SHA], base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()),
    "V10": _set(["tombstones", 0, "replaced_by"], "gone"),
    "V11": _set(["topics", 0, "text", "en", "title"], "<script>"),
}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_each_rule_names_itself_and_the_path(rule):
    b = _bundle()
    CASES[rule](b)
    problems = B.bundle_problems(b)
    assert problems and all(p.startswith(f"{rule} ") for p in problems), problems


@pytest.mark.parametrize("value", [None, [], "x", 7, {"schema": 1}])
def test_any_json_value_gives_problems_and_never_raises(value):
    assert B.bundle_problems(value)


def test_entry_problems_owns_the_text_limits():
    ok = ("t", "s", ["step"], None)
    assert B.entry_problems(*ok) == []
    assert B.entry_problems("x" * B.MAX_TITLE, "s", ["x" * B.MAX_STEP_TEXT] * B.MAX_STEPS, "2026-02-28") == []
    assert B.entry_problems("x" * (B.MAX_TITLE + 1), "s", ["a"], None)
    assert B.entry_problems("t", "s", ["x" * (B.MAX_STEP_TEXT + 1)], None)
    assert B.entry_problems("t", "s", ["a"] * (B.MAX_STEPS + 1), None)
    assert B.entry_problems("t", " ", ["a"], None)
    assert B.entry_problems("t", "s", [], None)
    assert B.entry_problems("t", "s", ["a"], "2026-02-30")
    assert B.entry_problems(1, None, "steps", 5)


def test_schema_has_no_copy_of_a_limit():
    from celerp.help import schema
    source = Path(schema.__file__).read_text(encoding="utf-8")
    for name in ("MAX_TITLE", "MAX_STEP_TEXT", "MAX_STEPS", "MAX_IMAGE_BYTES", "TOPIC_ID", "is_webp"):
        assert not re.search(rf"^(def )?{name}\b", source, re.M), name
        assert getattr(schema, name, getattr(B, name)) is getattr(B, name), name
    assert not hasattr(schema, "_is_webp") and not hasattr(schema, "_ID")


def test_the_validator_runs_alone_from_its_source_with_no_site():
    """As the site's wheel_check.py runs it: exec'd alone, one argument, no env."""
    source = Path(B.__file__).read_text(encoding="utf-8")
    module = types.ModuleType("celerp_help_bundle")
    exec(compile(source, "bundle.py", "exec"), module.__dict__)
    assert module.bundle_problems(json.loads(json.dumps(_bundle()))) == []


def test_a_named_site_without_check_bundle_is_one_problem(tmp_path):
    assert B.bundle_problems(_bundle(), site=tmp_path) == [
        f"IF14: no site checkout at {tmp_path} (website/check_bundle.py is missing)"]


def _site(tmp_path, body):
    (tmp_path / "website").mkdir(parents=True)
    (tmp_path / "website" / "check_bundle.py").write_text(body, encoding="utf-8")
    return tmp_path


def test_the_site_check_runs_on_the_bundle_and_its_lines_are_problems(tmp_path, monkeypatch):
    site = _site(tmp_path, "import json, sys\n"
                           "b = json.load(open(sys.argv[1]))\n"
                           "print('build: page for', b['topics'][0]['id']); sys.exit(1)\n")
    monkeypatch.setenv("CELERP_SITE", str(site))
    assert B.bundle_problems(_bundle()) == ["IF14 build: page for core.settings"]
    ok = _site(tmp_path / "ok", "import sys; sys.exit(0)\n")
    assert B.bundle_problems(_bundle(), site=ok) == []


def test_a_site_check_that_cannot_finish_is_one_problem(tmp_path, monkeypatch):
    site = _site(tmp_path, "import time; time.sleep(5)\n")
    monkeypatch.setattr(B, "SITE_CHECK_TIMEOUT", 0.5)
    assert B.bundle_problems(_bundle(), site=site) == [
        f"IF14: {site / 'website' / 'check_bundle.py'} ran past 0.5 seconds"]


def test_the_site_check_runs_only_on_a_bundle_table_v_accepts(tmp_path):
    site = _site(tmp_path, "import sys; print('ran'); sys.exit(1)\n")
    b = _bundle()
    b["version"] = "x"
    assert B.bundle_problems(b, site=site) == ["V2 version: must be a release version like 2.10.0"]

