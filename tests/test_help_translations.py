"""Guide translations: check_translations holds every catalog to the bundle rules in app CI,
and help_translations.py exports what needs translating and applies it back only when
the whole file is valid, catalogs first and the ledger last."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from celerp.help import checks, ledger, translations

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "help_translations.py"
_spec = importlib.util.spec_from_file_location("help_translations", _SCRIPT)
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


@pytest.fixture(scope="module")
def shipped():
    return {p.data["id"]: p.data for p in checks.shipped_pages() if isinstance(p.data, dict)}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.delenv("CELERP_SITE", raising=False)
    monkeypatch.setattr(translations, "TRANSLATIONS_DIR", tmp_path / "translations")
    led = tmp_path / "ledger.json"
    led.write_text(ledger.LEDGER_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(ledger, "LEDGER_FILE", led)


def _ids(shipped, n=2):
    return sorted(shipped)[:n]


def _entry(data, **over):
    entry = {"source": translations.source_hash(data), "title": "T " + data["title"], "summary": "S",
             "steps": [s["text"] for s in data["steps"]], "updated": "2026-11-03"}
    entry.update(over)
    return entry


def _with_th():
    data = {**ledger.ledger(), "locales": ["en", "th"]}
    ledger.write_ledger(data)


def _site(tmp_path, marker):
    """A site checkout whose check_bundle.py rejects any bundle holding ``marker``."""
    site = tmp_path / "site"
    (site / "website").mkdir(parents=True)
    (site / "website" / "check_bundle.py").write_text(
        "import sys\n"
        f"if {marker!r} in open(sys.argv[1], encoding='utf-8').read():\n"
        "    print('ci_check: rule 9 rejects the text'); sys.exit(1)\n", encoding="utf-8")
    return site


# check_translations (IF3)

def test_check_translations_accepts_fresh_and_stale_entries(shipped, monkeypatch):
    a, b = _ids(shipped)
    _with_th()
    translations.write_catalog("th", {a: _entry(shipped[a]), b: _entry(shipped[b], source="0" * 64)})
    assert checks.check_translations(list(checks.shipped_pages())) == []


def test_check_translations_names_a_bad_catalog(shipped, tmp_path):
    translations.TRANSLATIONS_DIR.mkdir()
    (translations.TRANSLATIONS_DIR / "th.json").write_text("{nope", encoding="utf-8")
    problems = checks.check_translations(checks.shipped_pages())
    assert len(problems) == 1 and problems[0].startswith("th.json is not valid JSON")


def test_check_translations_names_unknown_keys_and_entry_problems(shipped):
    a = _ids(shipped, 1)[0]
    n = len(shipped[a]["steps"])
    translations.write_catalog("th", {"core.nope": _entry(shipped[a]),
                                      a: _entry(shipped[a], steps=["x"] * (n + 1))})
    assert sorted(checks.check_translations(checks.shipped_pages())) == sorted([
        "th.json: core.nope is not a live page id",
        f"th.json: {a}: has {n + 1} steps, the English has {n}"])


def test_check_translations_names_duplicate_titles(shipped):
    a, b = _ids(shipped)
    translations.write_catalog("th", {a: _entry(shipped[a], title="Same"), b: _entry(shipped[b], title="Same")})
    assert checks.check_translations(checks.shipped_pages()) == [f"th.json: {b}: its title is also the title of {a}"]


def test_check_translations_runs_the_bundle_rules_and_the_site_check(shipped, monkeypatch, tmp_path):
    a = _ids(shipped, 1)[0]
    _with_th()
    translations.write_catalog("th", {a: _entry(shipped[a], title="T <b>")})
    problems = checks.check_translations(checks.shipped_pages())
    assert problems and all(p.startswith("help.json: V") for p in problems)
    translations.write_catalog("th", {a: _entry(shipped[a], title="T — em dash")})
    monkeypatch.setenv("CELERP_SITE", str(_site(tmp_path, "—")))
    assert checks.check_translations(checks.shipped_pages()) == ["help.json: IF14 ci_check: rule 9 rejects the text"]


def test_check_translations_holds_the_english_to_the_site_check(shipped, monkeypatch, tmp_path):
    a = _ids(shipped, 1)[0]
    monkeypatch.setenv("CELERP_SITE", str(_site(tmp_path, shipped[a]["summary"])))
    assert checks.check_translations(checks.shipped_pages()) == ["help.json: IF14 ci_check: rule 9 rejects the text"]


# help_translations.py (IF5)

def _todo(tmp_path, langs="th,es"):
    out = tmp_path / "todo.json"
    assert cli.main(["todo", "--langs", langs, "--out", str(out)]) == 0
    return out, json.loads(out.read_text(encoding="utf-8"))


def _fill(doc, page_id, lang, **over):
    slot = doc["entries"][page_id][lang]
    english = doc["entries"][page_id]["en"]
    slot.update({"title": "T " + english["title"], "summary": "S", "steps": list(english["steps"])}, **over)


def _write(path, doc):
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return path


def test_todo_lists_every_stale_or_absent_entry_with_its_english(shipped, tmp_path):
    a, b = _ids(shipped)
    translations.write_catalog("th", {a: _entry(shipped[a])})
    _, doc = _todo(tmp_path)
    assert doc["languages"] == ["th", "es"]
    assert set(doc["entries"]) == set(shipped)
    assert "th" not in doc["entries"][a] and set(doc["entries"][b]) == {"source", "en", "th", "es"}
    english = doc["entries"][b]["en"]
    assert english == {"title": shipped[b]["title"], "summary": shipped[b]["summary"],
                       "steps": [s["text"] for s in shipped[b]["steps"]]}
    assert doc["entries"][b]["th"] == {"title": "", "summary": "", "steps": [""] * len(english["steps"])}
    assert doc["entries"][b]["source"] == translations.source_hash(shipped[b])


@pytest.mark.parametrize("langs", ["en", "th,xx", "th,"])
def test_todo_refuses_a_code_that_is_not_a_site_language(tmp_path, capsys, langs):
    out = tmp_path / "todo.json"
    assert cli.main(["todo", "--langs", langs, "--out", str(out)]) == 1
    assert not out.exists()
    bad = [c for c in langs.split(",") if c != "th"][0]
    assert repr(bad) in capsys.readouterr().err


def test_todo_names_an_unwritable_output(tmp_path, capsys):
    out = tmp_path / "missing" / "todo.json"
    assert cli.main(["todo", "--langs", "th", "--out", str(out)]) == 1
    assert str(out) in capsys.readouterr().err
    assert not translations.TRANSLATIONS_DIR.exists()


def test_apply_writes_filled_slots_then_the_ledger_and_converges(shipped, tmp_path):
    a, b = _ids(shipped)
    path, doc = _todo(tmp_path)
    _fill(doc, a, "th")
    _fill(doc, b, "es")
    assert cli.main(["apply", str(_write(path, doc))]) == 0
    th, es = translations.catalog("th"), translations.catalog("es")
    assert set(th) == {a} and set(es) == {b}
    assert th[a]["source"] == translations.source_hash(shipped[a])
    assert translations.entry_state(shipped[a], th[a]) == ("fresh", [])
    assert ledger.ledger()["locales"] == ["en", "th", "es"]
    assert checks.check_ledger(checks.shipped_pages()) == []
    assert checks.check_translations(checks.shipped_pages()) == []
    before = (translations.TRANSLATIONS_DIR / "th.json").read_bytes(), ledger.LEDGER_FILE.read_bytes()
    th[a]["updated"] = "2026-01-01"
    translations.write_catalog("th", th)
    assert cli.main(["apply", str(path)]) == 0
    assert translations.catalog("th")[a]["updated"] == "2026-01-01"
    assert ledger.LEDGER_FILE.read_bytes() == before[1]


def _refused(path, capsys, *names):
    catalogs = sorted(p.name for p in translations.TRANSLATIONS_DIR.glob("*.json")) \
        if translations.TRANSLATIONS_DIR.exists() else []
    led = ledger.LEDGER_FILE.read_bytes()
    assert cli.main(["apply", str(path)]) == 1
    err = capsys.readouterr().err
    for name in names:
        assert name in err
    assert ledger.LEDGER_FILE.read_bytes() == led
    now = sorted(p.name for p in translations.TRANSLATIONS_DIR.glob("*.json")) \
        if translations.TRANSLATIONS_DIR.exists() else []
    assert now == catalogs
    return err


def test_apply_refuses_a_file_that_is_not_json(tmp_path, capsys):
    path = tmp_path / "todo.json"
    path.write_text("{nope", encoding="utf-8")
    _refused(path, capsys, str(path))


def test_apply_refuses_an_unknown_language_or_page(shipped, tmp_path, capsys):
    a = _ids(shipped, 1)[0]
    path, doc = _todo(tmp_path, "th")
    _fill(doc, a, "th")
    _refused(_write(path, {**doc, "languages": ["th", "xx"]}), capsys, "'xx'")
    doc["entries"]["core.nope"] = doc["entries"].pop(a)
    _refused(_write(path, doc), capsys, "core.nope")


def test_apply_refuses_an_entry_problem_and_writes_nothing(shipped, tmp_path, capsys):
    a, b = _ids(shipped)
    path, doc = _todo(tmp_path, "th")
    _fill(doc, a, "th")
    _fill(doc, b, "th", steps=doc["entries"][b]["en"]["steps"][:-1])
    _refused(_write(path, doc), capsys, f"{b} th: has")


def test_apply_refuses_english_that_changed_since_the_todo(shipped, tmp_path, capsys):
    a = _ids(shipped, 1)[0]
    path, doc = _todo(tmp_path, "th")
    _fill(doc, a, "th")
    doc["entries"][a]["source"] = "0" * 64
    _refused(_write(path, doc), capsys, f"{a}: the English changed")


@pytest.mark.parametrize("over, named", [
    ({"title": "x" * 120}, None), ({"title": "x" * 121}, "title"),
    ({"summary": ""}, "summary"), ({"title": 7}, "title"), ({"title": "a <b>"}, "V11"),
])
def test_apply_holds_each_text_limit(shipped, tmp_path, capsys, over, named):
    a = _ids(shipped, 1)[0]
    path, doc = _todo(tmp_path, "th")
    _fill(doc, a, "th", **over)
    if named is None:
        assert cli.main(["apply", str(_write(path, doc))]) == 0
    else:
        _refused(_write(path, doc), capsys, named)


def test_apply_refuses_what_the_site_check_rejects(shipped, tmp_path, monkeypatch, capsys):
    a = _ids(shipped, 1)[0]
    path, doc = _todo(tmp_path, "th")
    _fill(doc, a, "th", summary="S — dash")
    monkeypatch.setenv("CELERP_SITE", str(_site(tmp_path, "—")))
    _refused(_write(path, doc), capsys, "rule 9")


def test_apply_part_way_failure_leaves_the_ledger_last_and_a_rerun_converges(shipped, tmp_path, monkeypatch, capsys):
    a, b = _ids(shipped)
    path, doc = _todo(tmp_path)
    _fill(doc, a, "th")
    _fill(doc, b, "es")
    _write(path, doc)
    real = translations.write_catalog

    def fail_th(lang, entries):
        if lang == "th":
            raise OSError("read-only")
        real(lang, entries)
    monkeypatch.setattr(translations, "write_catalog", fail_th)
    assert cli.main(["apply", str(path)]) == 1
    assert "th.json" in capsys.readouterr().err
    assert ledger.ledger()["locales"] == ["en"]
    monkeypatch.setattr(translations, "write_catalog", real)
    assert cli.main(["apply", str(path)]) == 0
    assert ledger.ledger()["locales"] == ["en", "th", "es"]
    assert checks.check_ledger(checks.shipped_pages()) == []
