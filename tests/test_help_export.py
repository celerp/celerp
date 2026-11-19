"""The help bundle is assembled from the shipped guides, their fresh translations and
the ledger, and passes the bundle validator; catalogs and the ledger fail by name."""
from __future__ import annotations

import pytest

from celerp.help import assemble, ledger, translations
from celerp.help.assemble import build_bundle, git_date
from celerp.help.bundle import bundle_problems
from celerp.help.registry import get_index

PAGE = {"title": "Add {x}", "summary": "S", "steps": [{"text": "Click {new}."}, {"text": "Save."}]}


def _entry(**over):
    entry = {"source": translations.source_hash(PAGE), "title": "เพิ่ม", "summary": "ส",
             "steps": ["คลิก {new}", "บันทึก"], "updated": "2026-11-02"}
    entry.update(over)
    return entry


@pytest.fixture(autouse=True)
def _no_site(monkeypatch, tmp_path):
    monkeypatch.delenv("CELERP_SITE", raising=False)
    monkeypatch.setattr(translations, "TRANSLATIONS_DIR", tmp_path / "translations")


def test_entry_state_absent_stale_fresh():
    assert translations.entry_state(PAGE, None) == ("absent", [])
    assert translations.entry_state(PAGE, _entry(source="0" * 64)) == ("stale", [])
    assert translations.entry_state(PAGE, _entry()) == ("fresh", [])
    state, problems = translations.entry_state(PAGE, _entry(steps=["คลิก", "บันทึก"]))
    assert state == "fresh" and problems == ["step 1: placeholders differ from the English"]
    assert translations.entry_state(PAGE, _entry(steps=["คลิก {new}"]))[1] == ["has 1 steps, the English has 2"]
    changed = {**PAGE, "summary": "Other"}
    assert translations.entry_state(changed, _entry())[0] == "stale"


def test_catalog_and_ledger_name_a_bad_file(tmp_path, monkeypatch):
    assert translations.catalog("th") == {}
    translations.write_catalog("th", {"core.x": _entry()})
    assert translations.catalog("th") == {"core.x": _entry()}
    (tmp_path / "translations" / "th.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="th.json"):
        translations.catalog("th")
    monkeypatch.setattr(ledger, "LEDGER_FILE", tmp_path / "ledger.json")
    with pytest.raises(ValueError, match="ledger.json is missing"):
        ledger.ledger()
    (tmp_path / "ledger.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="ledger.json"):
        ledger.ledger()


def test_a_failed_write_keeps_the_old_catalog(monkeypatch):
    translations.write_catalog("th", {"core.x": _entry()})

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr("celerp.config_store._replace", boom)
    with pytest.raises(OSError):
        translations.write_catalog("th", {})
    assert translations.catalog("th") == {"core.x": _entry()}
    assert [p.name for p in translations.TRANSLATIONS_DIR.iterdir()] == ["th.json"]


def _fresh(page_id, lang="th"):
    page = get_index().pages[page_id]
    data = {"title": page.title, "summary": page.summary, "steps": page.steps}
    return {"source": translations.source_hash(data), "title": "T " + page.title, "summary": "S",
            "steps": [s["text"] for s in page.steps], "updated": "2026-11-03"}


def test_the_bundle_of_the_shipped_guides_is_valid(monkeypatch):
    bundle = build_bundle(lambda path: "2026-11-01")
    assert bundle_problems(bundle) == []
    shipped = {i for i, p in get_index().pages.items() if p.source in ("core", "first_party")}
    assert {t["id"] for t in bundle["topics"]} == shipped == set(ledger.ledger()["topics"])
    assert bundle["modules"][0]["name"] == "core"


def test_only_fresh_translations_and_ledger_tombstones_enter_the_bundle(monkeypatch):
    page_id, other = sorted(get_index().pages)[:2]
    translations.write_catalog("th", {page_id: _fresh(page_id), other: {**_fresh(other), "source": "0" * 64}})
    data = {**ledger.ledger(), "locales": ["en", "th"]}
    data["topics"] = {**data["topics"], "core.gone": page_id}
    data["modules"] = {"retired": ""}
    monkeypatch.setattr(assemble, "ledger", lambda: data)
    bundle = build_bundle(lambda path: "2026-11-01")
    text = {t["id"]: t["text"] for t in bundle["topics"]}
    assert "th" in text[page_id] and "th" not in text[other]
    assert {"id": "core.gone", "kind": "topic", "replaced_by": page_id} in bundle["tombstones"]
    assert {"id": "retired", "kind": "module", "replaced_by": ""} in bundle["tombstones"]
    assert bundle_problems(bundle) == []
    over = build_bundle(lambda path: "2026-11-01", overlay={"es": {other: _fresh(other, "es")}})
    assert "es" in over["locales"] and "es" in {t["id"]: t["text"] for t in over["topics"]}[other]


def test_git_date_refuses_a_shallow_clone_or_a_file_with_no_commit(monkeypatch):
    answers = {"rev-parse": "true", "log": ""}
    monkeypatch.setattr(assemble, "_git", lambda *args: answers[args[0]])
    with pytest.raises(RuntimeError, match="shallow"):
        git_date("x.json")
    answers["rev-parse"] = "false"
    with pytest.raises(RuntimeError, match="no commit"):
        git_date("x.json")
    answers["log"] = "2026-11-04"
    assert git_date("x.json") == "2026-11-04"
