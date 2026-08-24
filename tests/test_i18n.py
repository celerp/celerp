# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Unit tests for ui.i18n - translation infrastructure."""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------

# Force non-cached mode so lru_cache doesn't interfere across tests
os.environ.pop("CELERP_DEBUG_I18N", None)
from ui.i18n import t, _LOCALES_DIR


# ---------------------------------------------------------------------------
# Basic lookup
# ---------------------------------------------------------------------------

def test_basic_lookup():
    assert t("btn.save", "en") == "Save"


def test_basic_lookup_nav():
    assert t("nav.dashboard", "en") == "Dashboard"


def test_basic_lookup_page():
    assert t("page.inventory", "en") == "Inventory"


def test_basic_lookup_th():
    assert t("th.name", "en") == "Name"


def test_basic_lookup_label():
    assert t("label.sku", "en") == "SKU"


# ---------------------------------------------------------------------------
# Unknown locale fallback to English
# ---------------------------------------------------------------------------

def test_unknown_locale_falls_back_to_english():
    """A locale code that doesn't exist ('xx') must fall back to English."""
    assert t("btn.save", "xx") == "Save"


def test_unknown_locale_falls_back_all_keys():
    """Every key in en.json should be reachable via an unknown locale."""
    en = json.loads((Path(_LOCALES_DIR) / "en.json").read_text())
    for key in list(en.keys())[:20]:  # spot-check first 20
        assert t(key, "xx") == en[key]


# ---------------------------------------------------------------------------
# Missing key returns the key itself (no crash)
# ---------------------------------------------------------------------------

def test_missing_key_returns_key():
    result = t("nonexistent.key.xyz", "en")
    assert result == "nonexistent.key.xyz"


def test_missing_key_in_unknown_locale():
    result = t("nonexistent.key.xyz", "xx")
    assert result == "nonexistent.key.xyz"


# ---------------------------------------------------------------------------
# Parameterised interpolation
# ---------------------------------------------------------------------------

def test_interpolation_count():
    result = t("flash.items_transferred", "en", count=3)
    assert result == "3 item(s) transferred."


def test_interpolation_import():
    result = t("flash.import_complete", "en", created=10, skipped=2)
    assert result == "Import complete: 10 created, 2 skipped."


def test_interpolation_items_updated():
    result = t("flash.items_updated", "en", count=5, status="active")
    assert result == "5 item(s) updated to 'active'."


def test_interpolation_items_deleted():
    result = t("flash.items_deleted", "en", count=7)
    assert result == "7 item(s) deleted."


# ---------------------------------------------------------------------------
# en.json validation
# ---------------------------------------------------------------------------

def test_en_json_exists():
    path = Path(_LOCALES_DIR) / "en.json"
    assert path.exists(), "ui/locales/en.json must exist"


def test_en_json_is_valid_json():
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    assert isinstance(data, dict)


def test_en_json_no_empty_values():
    """All keys in en.json must have non-empty string values."""
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    empty_keys = [k for k, v in data.items() if not isinstance(v, str) or not v.strip()]
    assert empty_keys == [], f"Keys with empty values: {empty_keys}"


def test_en_json_has_required_namespaces():
    """en.json must have at least one key per expected namespace."""
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    namespaces = {k.split(".")[0] for k in data}
    required = {"nav", "btn", "label", "th", "page", "flash", "error", "msg"}
    missing = required - namespaces
    assert not missing, f"Missing namespaces in en.json: {missing}"


def test_en_json_key_format():
    """All keys must follow namespace.name convention (dot-separated)."""
    path = Path(_LOCALES_DIR) / "en.json"
    data = json.loads(path.read_text())
    bad_keys = [k for k in data if "." not in k]
    assert bad_keys == [], f"Keys without namespace: {bad_keys}"


# ---------------------------------------------------------------------------
# Interpolation edge cases
# ---------------------------------------------------------------------------

def test_no_kwargs_returns_raw_value():
    """Calling t() without kwargs must not attempt .format() on the string."""
    # "Save" has no braces — should work fine
    assert t("btn.save", "en") == "Save"


def test_kwargs_with_missing_placeholder_raises():
    """If the template has {count} but we pass wrong kwarg, KeyError is expected."""
    with pytest.raises(KeyError):
        t("flash.items_transferred", "en", wrong_kwarg=3)


def test_missing_key_with_kwargs_returns_key_formatted():
    """Missing key with kwargs — key itself is returned (no format attempted since key has no braces)."""
    result = t("nonexistent.key.xyz", "en", count=5)
    assert result == "nonexistent.key.xyz"


# ---------------------------------------------------------------------------
# Amharic (am) locale invariants
#
# am.json is a full mirror of en.json's keyset with native Amharic values.
# These invariants guard the two hazards of adding a locale: a keyset drift
# that silently drops UI copy, and a {name} placeholder mismatch that makes
# t().format() raise KeyError at render time for am users.
# ---------------------------------------------------------------------------

import string


def _load_locale(code: str) -> dict:
    path = Path(_LOCALES_DIR) / f"{code}.json"
    assert path.exists(), f"ui/locales/{code}.json must exist"
    return json.loads(path.read_text())


def _placeholders(value: str) -> set[str]:
    """Named {placeholder} fields in a t() template, matching str.format()."""
    return {
        field for _, field, _, _ in string.Formatter().parse(value)
        if field is not None
    }


def _placeholder_counts(value: str) -> Counter:
    """Named {placeholder} fields WITH their occurrence counts, matching
    str.format(). A duplicated, omitted, or added placeholder relative to en
    all change this multiset, so equality here is stricter than set equality
    and catches the duplicate-{author} class of bug that a set would miss."""
    return Counter(
        field for _, field, _, _ in string.Formatter().parse(value)
        if field is not None
    )


# ---------------------------------------------------------------------------
# Cross-locale parity (V1): a shipped locale must render correctly, and its
# completeness is a graded release signal, not merely "it does not crash".
#
# Two tiers:
#   CORRECTNESS - hard for EVERY shipped locale. An extra key is untranslatable
#     dead weight drifting from en; a {placeholder} set that differs from en
#     makes t(key, code, **kwargs).format() raise KeyError at render time.
#     Neither falls back safely, so both are bugs regardless of the language.
#   COMPLETENESS - a locale missing an en key falls back to English via t(),
#     which is safe, so partial coverage is a release-readiness signal, not a
#     crash. It is a HARD gate only for locales declared release-complete in
#     _COMPLETE_LOCALES; for every other shipped locale the missing-key count
#     is reported (a warning), not failed. Membership of _COMPLETE_LOCALES is
#     the release cutover that separates "safe fallback" from "shippable"; a
#     locale is added once it is fully translated.
# ---------------------------------------------------------------------------

# Locales whose keyset completeness is enforced as a hard CI gate. English is
# the source of truth and always complete; a translated locale joins this set
# when it reaches full parity, after which any future missing key fails CI.
_COMPLETE_LOCALES = frozenset(
    {"en", "am", "ar", "de", "es", "fr", "id", "it", "ja", "pt", "th", "vi"}
)


def _shipped_locales() -> list[str]:
    """Every central locale shipped on disk (ui/locales/<code>.json)."""
    return sorted(p.stem for p in Path(_LOCALES_DIR).glob("*.json"))


def test_every_shipped_locale_has_no_extra_keys():
    """CORRECTNESS (hard, all locales): no locale may carry a key absent from
    en.json - an extra key is dead weight and a sign of drift from the source."""
    en = set(_load_locale("en"))
    offenders = {
        code: sorted(set(_load_locale(code)) - en)
        for code in _shipped_locales()
        if set(_load_locale(code)) - en
    }
    assert not offenders, f"Locales with keys absent from en.json: {offenders}"


def test_every_shipped_locale_placeholder_parity():
    """CORRECTNESS (hard, all locales): for every key a locale defines, its
    {placeholder} MULTISET must equal en's (Counter, not set), or
    t(key, code, **kwargs).format() raises KeyError at render time, or a
    duplicated placeholder silently doubles interpolated content. Counting
    occurrences catches a placeholder that is duplicated, omitted, or added."""
    en = _load_locale("en")
    mismatched = {}
    for code in _shipped_locales():
        if code == "en":
            continue
        loc = _load_locale(code)
        bad = {
            key: (dict(_placeholder_counts(en[key])), dict(_placeholder_counts(loc[key])))
            for key in loc
            if key in en and _placeholder_counts(en[key]) != _placeholder_counts(loc[key])
        }
        if bad:
            mismatched[code] = bad
    assert not mismatched, f"Placeholder multiset mismatch vs en per locale: {mismatched}"


def test_release_complete_locales_have_full_keyset():
    """COMPLETENESS gate (hard, _COMPLETE_LOCALES only): every locale declared
    release-complete must define every en.json key. This flips a locale from
    'safe fallback' to 'shippable'; add a locale here only once it is fully
    translated, and this gate keeps it complete thereafter."""
    en = set(_load_locale("en"))
    incomplete = {
        code: len(en - set(_load_locale(code)))
        for code in sorted(_COMPLETE_LOCALES)
        if code != "en" and (en - set(_load_locale(code)))
    }
    assert not incomplete, (
        f"Release-complete locales missing en keys (count per locale): {incomplete}. "
        "Finish the translation, or drop the locale from _COMPLETE_LOCALES."
    )


def test_locale_completeness_report():
    """COMPLETENESS report (warns, never fails on partial coverage): surfaces
    how many en keys each not-yet-complete shipped locale is missing, so the
    release gap is visible in CI output. Missing keys fall back to English, so
    this is a readiness signal; the hard gate is
    test_release_complete_locales_have_full_keyset. A locale that reaches ZERO
    missing keys is flagged as ready to promote onto _COMPLETE_LOCALES."""
    import warnings
    en = set(_load_locale("en"))
    report = {
        code: len(en - set(_load_locale(code)))
        for code in _shipped_locales()
        if code not in _COMPLETE_LOCALES
    }
    if report:
        ready = sorted(c for c, n in report.items() if n == 0)
        warnings.warn(
            f"locale completeness (en keys missing): {report}"
            + (f"; ready to promote to _COMPLETE_LOCALES: {ready}" if ready else ""),
            stacklevel=2,
        )


def test_every_shipped_locale_values_nonempty_valid():
    """Every shipped locale must be valid JSON with every value a non-empty
    string - an empty value renders blank instead of falling back to en."""
    for code in _shipped_locales():
        loc = _load_locale(code)
        assert isinstance(loc, dict), f"{code}.json is not a JSON object"
        bad = [k for k, v in loc.items() if not isinstance(v, str) or not v.strip()]
        assert bad == [], f"{code}.json keys with empty/non-string values: {bad}"


def test_am_no_em_dash():
    """No em dash (U+2014) in any am value; Amharic uses its own punctuation.

    Deliberately scoped to am, the content this branch introduces. en.json and
    every shipped locale already carry em dashes in a handful of legacy keys
    (e.g. label._none, inv._select_a_preset), so a suite-wide invariant would
    fail on pre-existing English copy unrelated to this change."""
    am = _load_locale("am")
    em_dash = "\u2014"  # U+2014 EM DASH
    offending = [k for k, v in am.items() if isinstance(v, str) and em_dash in v]
    assert offending == [], f"am.json values containing an em dash: {offending}"


# ---------------------------------------------------------------------------
# Source-equality: no untranslated UI prose left identical to English
#
# A value in a translated locale that is byte-identical to en is untranslated
# UI copy UNLESS the pair is a reviewed exception (a brand/product name, a
# URL/email, a shell command, a symbol/currency-only label, or an intentional
# acronym). Those exceptions are enumerated per (locale, key) in the reviewed
# allowlist file; there is deliberately NO blanket English-word exemption, so a
# newly added untranslated string fails until it is either translated or
# explicitly reviewed into the allowlist.
# ---------------------------------------------------------------------------

_SOURCE_IDENTICAL_ALLOWLIST = (
    Path(__file__).parent / "i18n_source_identical_allowlist.json"
)


def _load_source_identical_allowlist() -> dict[str, set[str]]:
    data = json.loads(_SOURCE_IDENTICAL_ALLOWLIST.read_text())
    return {code: set(keys) for code, keys in data.items()}


def test_no_untranslated_prose_in_complete_locales():
    """Every release-complete locale must translate every key: a value identical
    to en is only allowed when its (locale, key) pair is in the reviewed
    allowlist (brands, URLs, commands, symbols/currency, intentional acronyms).
    No global English-word exemption exists."""
    en = _load_locale("en")
    allow = _load_source_identical_allowlist()
    offenders = {}
    for code in sorted(_COMPLETE_LOCALES):
        if code == "en":
            continue
        loc = _load_locale(code)
        allowed = allow.get(code, set())
        identical = [
            k for k, v in loc.items()
            if k in en and v == en[k] and k not in allowed
        ]
        if identical:
            offenders[code] = sorted(identical)
    assert not offenders, (
        "Untranslated values identical to English (translate them, or add the "
        f"reviewed (locale, key) pair to {_SOURCE_IDENTICAL_ALLOWLIST.name}): {offenders}"
    )


def test_source_identical_allowlist_has_no_stale_entries():
    """The reviewed allowlist may not carry a (locale, key) that is no longer
    source-identical (the translation was added) or no longer exists - stale
    exceptions hide future regressions."""
    en = _load_locale("en")
    allow = _load_source_identical_allowlist()
    stale = {}
    for code, keys in allow.items():
        loc = _load_locale(code)
        bad = sorted(k for k in keys if k not in loc or k not in en or loc[k] != en[k])
        if bad:
            stale[code] = bad
    assert not stale, f"Stale allowlist entries (translated or removed): {stale}"


# ---------------------------------------------------------------------------
# Bundled module catalogs: release-complete locales are keyset-exact
#
# celerp-labels is a first-party bundled module. Every locale declared
# release-complete must ship the module's full keyset, exactly - no missing and
# no extra keys - the same bar as the central catalogs. Partial module catalogs
# are only acceptable for third-party modules explicitly allowed to be partial,
# which the bundled celerp-labels is not.
# ---------------------------------------------------------------------------

_BUNDLED_MODULE_LOCALE_DIRS = [
    Path(__file__).parent.parent
    / "default_modules" / "celerp-labels" / "celerp_labels" / "locales",
]


def test_bundled_module_locales_keyset_exact_for_complete_locales():
    """Every release-complete locale must have EXACTLY the module en.json keyset
    for each bundled module catalog (no missing, no extra keys)."""
    problems = {}
    for locdir in _BUNDLED_MODULE_LOCALE_DIRS:
        en_keys = set(json.loads((locdir / "en.json").read_text()))
        for code in sorted(_COMPLETE_LOCALES):
            if code == "en":
                continue
            path = locdir / f"{code}.json"
            if not path.exists():
                problems[f"{locdir.parent.name}:{code}"] = "missing catalog file"
                continue
            keys = set(json.loads(path.read_text()))
            missing = sorted(en_keys - keys)
            extra = sorted(keys - en_keys)
            if missing or extra:
                problems[f"{locdir.parent.name}:{code}"] = {
                    "missing": missing, "extra": extra
                }
    assert not problems, f"Bundled module catalog keyset drift: {problems}"


def test_bundled_module_locale_values_nonempty_with_placeholder_parity():
    """Bundled module locale values are non-empty and preserve the en
    placeholder multiset, exactly as the central catalogs are held."""
    bad = {}
    for locdir in _BUNDLED_MODULE_LOCALE_DIRS:
        en = json.loads((locdir / "en.json").read_text())
        for code in sorted(_COMPLETE_LOCALES):
            if code == "en":
                continue
            loc = json.loads((locdir / f"{code}.json").read_text())
            for k, v in loc.items():
                if not isinstance(v, str) or not v.strip():
                    bad[f"{code}:{k}"] = "empty/non-string"
                elif k in en and _placeholder_counts(v) != _placeholder_counts(en[k]):
                    bad[f"{code}:{k}"] = "placeholder mismatch"
    assert not bad, f"Bundled module locale value problems: {bad}"


# ---------------------------------------------------------------------------
# Terminology regressions: accounting false friends, line items, journal
# entries, and the fulfillment family must use the correct domain sense.
# ---------------------------------------------------------------------------

# Outstanding-balance false friends (the "remarkable/exceptional" sense) that
# must never appear in the outstanding-balance keys.
_OUTSTANDING_KEYS = ["doc.outstanding", "th.outstanding"]
_OUTSTANDING_FALSE_FRIENDS = {
    "ar": ["متميز"], "fr": ["Exceptionnel"], "id": ["Luar biasa"],
    "ja": ["並外れた"], "pt": ["Fora do comum"], "th": ["โดดเด่น"], "vi": ["Nổi bật"],
}

# Line-item false friends (the advertising "line item" sense).
_LINE_ITEM_KEYS = ["page.line_items"]
_LINE_ITEM_FALSE_FRIENDS = {
    "fr": ["Éléments de campagne"], "es": ["Artículos de línea"], "th": ["รายการโฆษณา"],
}

# Keys whose English prose contains "fulfil" in a non-order-fulfillment sense:
# a work-order hint sentence and a marketplace privacy note about performing a
# purchase. They are not fulfillment-status UI labels, so the order-fulfillment
# terminology check must not sweep them in.
_FULFILL_NON_FAMILY_PROSE = {
    "inventory.work_orders_hint",
    "marketplace.third_party_data_note",
}


def _fulfillment_family_keys(en: dict) -> list[str]:
    explicit = {
        "btn.fulfill_deduct_inventory", "btn.revert_fulfillment", "doc.fulfilled",
        "doc.partially_fulfilled", "status.unfulfilled", "event.item.fulfilled",
        "event.item.fulfillment_reversed", "event.doc.fulfilled",
        "event.doc.partially_fulfilled", "event.doc.fulfillment_reversed",
        "activity.change.fulfilled_items",
    }
    return sorted(
        k for k in en
        if k not in _FULFILL_NON_FAMILY_PROSE
        and (k in explicit or "fulfil" in en[k].lower())
    )


def test_outstanding_balance_false_friends_absent():
    """No outstanding-balance key uses the 'remarkable/exceptional' false friend."""
    hits = {}
    for code, terms in _OUTSTANDING_FALSE_FRIENDS.items():
        loc = _load_locale(code)
        for key in _OUTSTANDING_KEYS:
            v = loc.get(key, "")
            found = [tm for tm in terms if tm in v]
            if found:
                hits[f"{code}:{key}"] = found
    assert not hits, f"Outstanding-balance false friends present: {hits}"


def test_line_item_false_friends_absent():
    """No line-items key uses the advertising 'line item' false friend."""
    hits = {}
    for code, terms in _LINE_ITEM_FALSE_FRIENDS.items():
        loc = _load_locale(code)
        for key in _LINE_ITEM_KEYS:
            v = loc.get(key, "")
            found = [tm for tm in terms if tm in v]
            if found:
                hits[f"{code}:{key}"] = found
    assert not hits, f"Line-item false friends present: {hits}"


# A blacklist of wrong synonyms is inadequate: a different wrong synonym slips
# through it. The fulfillment family is instead pinned to its exact reviewed
# translation, per (locale, key), for the five locales this branch reworked.
# event.item.fulfilled ("Sold") and event.item.fulfillment_reversed ("Sale
# reversed") are the SALE-level pair and keep the sale sense; the doc-level keys
# carry the order-processing sense.
_FULFILL_EXPECTED = {
    "am": {
        "btn.fulfill_deduct_inventory": "ትዕዛዝ ፈጽም / ክምችት ቀንስ",
        "btn.revert_fulfillment": "የትዕዛዝ አፈጻጸምን ቀልብስ",
        "doc.fulfilled": "ተፈጽሟል",
        "doc.partially_fulfilled": "በከፊል ተፈጽሟል",
        "status.unfulfilled": "ያልተፈጸመ",
        "event.item.fulfilled": "ተሽጧል",
        "event.item.fulfillment_reversed": "ሽያጭ ተቀልብሷል",
        "event.doc.fulfilled": "ተፈጽሟል",
        "event.doc.partially_fulfilled": "በከፊል ተፈጽሟል",
        "event.doc.fulfillment_reversed": "የትዕዛዝ አፈጻጸም ተቀልብሷል",
        "activity.change.fulfilled_items": "የተፈጸሙ እቃዎች ተዘምነዋል",
    },
    "ar": {
        "btn.fulfill_deduct_inventory": "تنفيذ الطلب / خصم المخزون",
        "btn.revert_fulfillment": "التراجع عن تنفيذ الطلب",
        "doc.fulfilled": "تم تنفيذه",
        "doc.partially_fulfilled": "تم تنفيذه جزئيًا",
        "status.unfulfilled": "لم يُنفَّذ",
        "event.item.fulfilled": "مباع",
        "event.item.fulfillment_reversed": "تم التراجع عن البيع",
        "event.doc.fulfilled": "تم تنفيذه",
        "event.doc.partially_fulfilled": "تم تنفيذه جزئيًا",
        "event.doc.fulfillment_reversed": "تم التراجع عن تنفيذ الطلب",
        "activity.change.fulfilled_items": "تم تحديث العناصر المُنفَّذة",
    },
    "es": {
        "btn.fulfill_deduct_inventory": "Procesar pedido / Descontar inventario",
        "btn.revert_fulfillment": "Revertir procesamiento",
        "doc.fulfilled": "Procesado",
        "doc.partially_fulfilled": "Procesado parcialmente",
        "status.unfulfilled": "Sin procesar",
        "event.item.fulfilled": "Vendido",
        "event.item.fulfillment_reversed": "Venta revertida",
        "event.doc.fulfilled": "Procesado",
        "event.doc.partially_fulfilled": "Procesado parcialmente",
        "event.doc.fulfillment_reversed": "Procesamiento revertido",
        "activity.change.fulfilled_items": "Artículos procesados actualizados",
    },
    "id": {
        "btn.fulfill_deduct_inventory": "Proses pesanan / Kurangi inventori",
        "btn.revert_fulfillment": "Batalkan pemrosesan pesanan",
        "doc.fulfilled": "Diproses",
        "doc.partially_fulfilled": "Diproses sebagian",
        "status.unfulfilled": "Belum diproses",
        "event.item.fulfilled": "Terjual",
        "event.item.fulfillment_reversed": "Penjualan dibatalkan",
        "event.doc.fulfilled": "Diproses",
        "event.doc.partially_fulfilled": "Diproses sebagian",
        "event.doc.fulfillment_reversed": "Pemrosesan dibatalkan",
        "activity.change.fulfilled_items": "Barang yang telah diproses diperbarui",
    },
    "pt": {
        "btn.fulfill_deduct_inventory": "Processar pedido / Deduzir estoque",
        "btn.revert_fulfillment": "Reverter processamento do pedido",
        "doc.fulfilled": "Processado",
        "doc.partially_fulfilled": "Processado parcialmente",
        "status.unfulfilled": "Não processado",
        "event.item.fulfilled": "Vendido",
        "event.item.fulfillment_reversed": "Venda revertida",
        "event.doc.fulfilled": "Processado",
        "event.doc.partially_fulfilled": "Processado parcialmente",
        "event.doc.fulfillment_reversed": "Processamento revertido",
        "activity.change.fulfilled_items": "Itens processados atualizados",
    },
}


def test_fulfillment_family_exact_values():
    """The reworked fulfillment family is pinned to its exact reviewed value per
    (locale, key). This replaces the wrong-synonym blacklist, which a different
    wrong synonym passes. Every family key must be covered for each locale."""
    en = _load_locale("en")
    fam = set(_fulfillment_family_keys(en))
    wrong = {}
    for code, kv in _FULFILL_EXPECTED.items():
        uncovered = fam - set(kv)
        assert not uncovered, f"{code}: fulfillment family keys not pinned: {sorted(uncovered)}"
        loc = _load_locale(code)
        for key, val in kv.items():
            if loc.get(key) != val:
                wrong[f"{code}:{key}"] = {"want": val, "got": loc.get(key)}
    assert not wrong, f"Fulfillment family not at reviewed values: {wrong}"


def test_mandated_accounting_terminology_applied():
    """The reviewed accounting corrections are present verbatim: outstanding
    balance, line items, empty-line-items, and create-journal-entry copy."""
    expected = {
        "ar": {"doc.outstanding": "مستحق:", "page.line_items": "البنود",
               "doc.no_line_items": "لا توجد بنود.", "page.create_journal_entry": "إنشاء قيد يومية"},
        "es": {"page.line_items": "Líneas", "doc.no_line_items": "No hay líneas.",
               "page.create_journal_entry": "Crear asiento contable"},
        "fr": {"doc.outstanding": "Solde dû :", "page.line_items": "Lignes",
               "doc.no_line_items": "Aucune ligne.", "page.create_journal_entry": "Créer une écriture comptable"},
        "id": {"doc.outstanding": "Belum dibayar:", "page.line_items": "Baris item",
               "doc.no_line_items": "Tidak ada baris item.", "page.create_journal_entry": "Buat entri jurnal"},
        "ja": {"doc.outstanding": "未決済：", "page.line_items": "明細行",
               "doc.no_line_items": "明細行はありません。", "page.create_journal_entry": "仕訳を作成"},
        "pt": {"doc.outstanding": "Em aberto:", "page.line_items": "Itens",
               "doc.no_line_items": "Nenhum item."},
        "th": {"doc.outstanding": "ยอดคงค้าง:", "page.line_items": "รายการ",
               "doc.no_line_items": "ไม่มีรายการ", "page.create_journal_entry": "บันทึกรายการสมุดรายวัน"},
        "vi": {"doc.outstanding": "Chưa thanh toán:", "page.line_items": "Dòng chi tiết",
               "doc.no_line_items": "Không có dòng chi tiết.", "page.create_journal_entry": "Tạo bút toán"},
    }
    wrong = {}
    for code, kv in expected.items():
        loc = _load_locale(code)
        for key, val in kv.items():
            if loc.get(key) != val:
                wrong[f"{code}:{key}"] = {"want": val, "got": loc.get(key)}
    assert not wrong, f"Mandated accounting terminology not applied: {wrong}"


# ---------------------------------------------------------------------------
# Translation-integrity regressions: glossary-marker residue, punctuation-only
# corruption, preserved technical literals, and Thai truncation. These guard the
# systemic failure modes of the machine-assisted translation pass.
# ---------------------------------------------------------------------------

# A prior translation pass substituted glossary terms with placeholder tokens
# (e.g. TERME5, TERMO5, المصطلح5, เทอม5, HẠN5 for "term") and failed to restore
# some. The stem alone (المصطلح = "the term") is a legitimate word; only the
# stem immediately followed by a digit, or any __TOKEN__ wrapper, is residue.
_MARKER_RESIDUE = re.compile(r"(?:TERME|TERMO|المصطلح|เทอม|HẠN)\d+|__[^\s_][^\s]*__")


def test_no_glossary_marker_residue():
    """No locale value carries a leaked glossary marker (stem+digit or __TOKEN__)."""
    hits = {}
    for code in _shipped_locales():
        loc = _load_locale(code)
        bad = sorted(k for k, v in loc.items() if isinstance(v, str) and _MARKER_RESIDUE.search(v))
        if bad:
            hits[code] = bad
    assert not hits, f"Glossary-marker residue present: {hits}"


# Values that are intentionally a symbol or mark only (no letters/digits), so
# the punctuation-only corruption check must not sweep them in.
_SYMBOL_ONLY_ALLOWED = {
    ("am", "doc.u2630"), ("de", "doc.u2630"), ("it", "doc.u2630"),
    ("ja", "table.daterange_to"),
}
_WORD_CHAR = re.compile(r"\w", re.UNICODE)


def test_no_punctuation_only_translations():
    """A value stripped of all letters/digits when its English source has letters
    is a corrupted (punctuation-only) translation, unless the (locale, key) pair
    is an explicit symbol-only exception."""
    en = _load_locale("en")
    hits = {}
    for code in _shipped_locales():
        if code == "en":
            continue
        loc = _load_locale(code)
        bad = sorted(
            k for k, v in loc.items()
            if isinstance(v, str) and k in en
            and _WORD_CHAR.search(en[k]) and not _WORD_CHAR.search(v)
            and (code, k) not in _SYMBOL_ONLY_ALLOWED
        )
        if bad:
            hits[code] = bad
    assert not hits, (
        f"Punctuation-only translations (translate them, or add the (locale, key) "
        f"pair to _SYMBOL_ONLY_ALLOWED): {hits}"
    )


# Technical literals in the English source (routes, CLI flags, module targets,
# URLs, the backup-file name) are code, not prose, and must survive verbatim in
# every locale.
_LITERAL_PATTERNS = [
    re.compile(r"https?://[^\s]+"),                          # URLs
    re.compile(r"/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-{}]+)+"),  # multi-segment routes
    re.compile(r"--[a-z][a-z0-9-]+"),                        # long CLI flags
    re.compile(r"\b[a-z_]+\.[a-z_]+:[a-z_]+\b"),             # module:app targets
    re.compile(r"\.celerp-backup\b"),                        # backup filename (leading dot)
    re.compile(r"\b[A-Z][A-Z0-9_]{2,}=[A-Za-z0-9_./\-]+"),   # environment assignments
    re.compile(r"\b[A-Za-z0-9_\-]*[A-Za-z][A-Za-z0-9_\-]*\.[A-Za-z][A-Za-z0-9]{1,4}\b"),  # filenames and domains
]


def _technical_literals(value: str) -> set[str]:
    out: set[str] = set()
    for pat in _LITERAL_PATTERNS:
        out |= set(pat.findall(value))
    return out


def test_technical_literals_preserved():
    """Commands, CLI flags, module targets, URLs, application routes, and the
    .celerp-backup filename in the English source appear verbatim in every locale
    value for that key. A translated route or filename is a broken instruction."""
    en = _load_locale("en")
    en_literals = {k: _technical_literals(v) for k, v in en.items() if _technical_literals(v)}
    hits = {}
    for code in _shipped_locales():
        if code == "en":
            continue
        loc = _load_locale(code)
        for key, lits in en_literals.items():
            v = loc.get(key, "")
            missing = sorted(x for x in lits if x not in v)
            if missing:
                hits[f"{code}:{key}"] = missing
    assert not hits, f"Technical literals lost in translation: {hits}"


# Thai values that collapsed to a bare fragment (only "แล้ว" = "already/done",
# losing the subject and verb) or a lone dash/mark are the known truncation bug.
_THAI_TRUNCATIONS = {"แล้ว", ".", "-", "ๆ", "…"}


def test_thai_no_truncated_values():
    """No Thai value is a bare truncated fragment; every value carries its full
    subject and verb, not just a trailing particle."""
    th = _load_locale("th")
    bad = sorted(k for k, v in th.items() if isinstance(v, str) and v.strip() in _THAI_TRUNCATIONS)
    assert not bad, f"Thai truncated to a bare fragment: {bad}"


def test_bundled_module_locales_no_untranslated_prose():
    """Bundled module catalogs get the same source-identical review as the
    central catalogs: no release-complete locale value may be byte-identical to
    the module's English source (there is currently no reviewed exception)."""
    offenders = {}
    for locdir in _BUNDLED_MODULE_LOCALE_DIRS:
        en = json.loads((locdir / "en.json").read_text())
        for code in sorted(_COMPLETE_LOCALES):
            if code == "en":
                continue
            loc = json.loads((locdir / f"{code}.json").read_text())
            identical = sorted(
                k for k, v in loc.items()
                if k in en and v == en[k] and en[k].strip()
            )
            if identical:
                offenders[f"{locdir.parent.name}:{code}"] = identical
    assert not offenders, f"Untranslated module prose identical to English: {offenders}"


# ---------------------------------------------------------------------------
# Topbar language switcher: native labels + searchable combobox
# ---------------------------------------------------------------------------

def _render_topbar() -> str:
    from fasthtml.common import to_xml
    from ui.components.shell import _topbar
    return to_xml(_topbar([], lang="en"))


def test_switcher_renders_native_labels():
    """The switcher shows each locale's native name, not its upper-case code."""
    html = _render_topbar()
    assert "ไทย" in html, "topbar switcher must render the native locale label 'ไทย', not 'TH'"


def test_switcher_is_searchable_combobox():
    """The switcher is the app's searchable_select combobox, not a native <select>."""
    html = _render_topbar()
    assert "combobox-input" in html, "topbar switcher must be the searchable combobox (combobox-input)"


def test_switcher_renders_native_amharic_label():
    """am.json ships with this branch, so the switcher must show its native name
    'አማርኛ' rather than the upper-case code 'AM'."""
    html = _render_topbar()
    assert "አማርኛ" in html, "topbar switcher must render the native Amharic label 'አማርኛ', not 'AM'"


def test_switcher_has_aria_combobox_semantics():
    """The switcher exposes ARIA combobox semantics (rule i): the visible input is
    a role=combobox owning a listbox of role=option items. The dynamic wiring
    (aria-controls / aria-expanded / aria-activedescendant) is added by
    initCombobox at runtime; these static roles are what the server must render."""
    html = _render_topbar()
    assert 'role="combobox"' in html, "combobox input must carry role=combobox"
    assert 'aria-haspopup="listbox"' in html, "combobox input must declare aria-haspopup=listbox"
    assert 'aria-autocomplete="list"' in html, "combobox input must declare aria-autocomplete=list"
    assert 'role="listbox"' in html, "the option list must carry role=listbox"
    assert 'role="option"' in html, "each option must carry role=option"
