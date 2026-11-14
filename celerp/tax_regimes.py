# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Country-based tax regime defaults.

Each country's regime comes from the release's country packs (``celerp.localization``), keyed by
ISO 3166-1 alpha-2 country code (uppercase). Used to seed company.settings["taxes"] and
company.settings["currency"] when a default location with a known country is first set.

Each regime:
  currency: str          ISO 4217 code
  taxes: list[dict]      a list the TaxRatesPatch schema in routers/companies.py accepts

``DEFAULT_REGIME`` is the fallback for an unknown or unset country.
"""
from __future__ import annotations

import copy

from celerp.localization import lock_codes, missing, packs
from celerp.services.list_behavior import SUPPLIER_DOC_TYPES

# The sides a tax is for; a row stored without one, or with a blank one, is for both.
TAX_TYPES = ("sales", "purchase", "both")

DEFAULT_REGIME: dict = {
    "currency": "USD",
    "taxes": [
        {"name": "Standard Tax", "rate": 0.0, "tax_type": "both", "is_default": True,
         "description": "Set your local tax rate", "is_compound": False, "default_order": 0},
        {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
         "description": "Tax-exempt", "is_compound": False, "default_order": 0},
    ],
}


def tax_countries() -> tuple[str, ...]:
    """The countries a company can name to start with the tax rates in force there: the lock's
    countries whose pack loaded, in lock order."""
    return tuple(packs())


def _code(value: str | None) -> str:
    return str(value or "").strip().upper()


def tax_country(value: str | None) -> str:
    """The country code ``value`` names when Celerp has its tax rates, else ""."""
    code = _code(value)
    return code if code in packs() else ""


def pack_missing(value: str | None) -> bool:
    """True when ``value`` names a country whose pack this installation lacks, or any country
    when the lock itself is unreadable: its stored taxes are kept, never replaced."""
    code = _code(value)
    if not code:
        return False
    return lock_codes() is None or code in missing()


def country_name(value: str, lang: str | None = None) -> str:
    """A country as people read it: the catalog name of a country code, else the value as
    written (an address may spell its country out)."""
    from ui.i18n import t
    key = f"country.{_code(value)}"
    name = t(key, lang)
    return value if name == key else name


def country_names(codes, lang: str | None = None) -> str:
    """Several countries as people read them, in one line."""
    return ", ".join(country_name(code, lang) for code in codes)


def missing_pack_refusal(value: str | None) -> str | None:
    """Why ``value`` cannot be chosen when its country's pack is missing, else None."""
    if not pack_missing(value):
        return None
    from ui.i18n import t
    if lock_codes() is None:
        return t("l10n.pack_lock_missing.body")
    return t("l10n.pack_missing.body", countries=country_name(value))


def get_regime(country_code: str | None) -> dict:
    """Return the tax regime for a country code (case-insensitive). Falls back to DEFAULT_REGIME."""
    return packs().get(_code(country_code), DEFAULT_REGIME)


# Which catalog Celerp set the company's sales taxes from: a tax country's code, or "" for the
# generic list. Present only while those taxes are untouched; any other write of them drops it.
TAXES_SEEDED_FOR = "taxes_seeded_for"


def with_taxes(settings: dict, taxes: list[dict], seeded_for: str | None = None) -> dict:
    """``settings`` with the company's sales taxes set to ``taxes``. ``seeded_for`` is the
    catalog Celerp set them from; a write by anyone else passes none, so the list is the
    company's own from then on and is never replaced automatically."""
    out = {**settings, "taxes": taxes}
    if seeded_for is None:
        out.pop(TAXES_SEEDED_FOR, None)
    else:
        out[TAXES_SEEDED_FOR] = seeded_for
    return out


def generic_taxes() -> list[dict]:
    """The neutral tax list a company starts from until it configures its own."""
    return copy.deepcopy(DEFAULT_REGIME["taxes"])


def configured(settings: dict, key: str, default: list[dict]) -> list[dict]:
    """The list saved under ``key``, an explicitly saved empty list included; a copy
    of ``default`` only when nothing was ever saved."""
    value = settings.get(key)
    return value if value is not None else copy.deepcopy(default)


def sales_taxes(settings: dict) -> list[dict]:
    """The company's sales taxes: what it saved, else the generic list."""
    return configured(settings, "taxes", generic_taxes())


def purchasing_taxes(settings: dict) -> list[dict]:
    """The company's purchasing taxes: what it saved, else a copy of its sales taxes."""
    return configured(settings, "purchasing_taxes", sales_taxes(settings))


def doc_side(doc_type: str | None) -> str:
    """The side a document's taxes apply to: "purchase" for a supplier document
    (SUPPLIER_DOC_TYPES), else "sales"."""
    return "purchase" if doc_type in SUPPLIER_DOC_TYPES else "sales"


def side_taxes(settings: dict, doc_type: str | None) -> list[dict]:
    """The tax list a document of this type draws from: purchasing taxes for a purchase
    document, sales taxes for any other."""
    return purchasing_taxes(settings) if doc_side(doc_type) == "purchase" else sales_taxes(settings)


def taxes_for(taxes: list[dict], doc_type: str | None) -> list[dict]:
    """The taxes a document of this type can carry: those for its side and those for both."""
    side = doc_side(doc_type)
    return [x for x in taxes if (x.get("tax_type") or "both") in (side, "both")]


def default_line_taxes(taxes: list[dict], doc_type: str | None, *, doc: dict | None) -> list[dict] | None:
    """The taxes list a new line starts with, whichever way it is added (scan, from items, append,
    reorder, the editor). `doc` is the target document's stored state, None for a new one.

    On a document with no document tax: every company default for the document's side with a
    rate that is not zero, in list order (a company may hold two, such as CGST 9% and SGST 9%),
    each as line_tax builds it. On a draft that already charges a tax as a whole, its taxation
    stands: a new line takes the taxes its lines all carry (as line_tax_key), copied whole, never
    a company default; a line saved with only a rate carries what legacy_line_taxes reads. When
    they differ or there are none, None: the line carries no tax of its own until the user sets one
    (the editor shows "--"); so does a line whose tax is itself still to be set or can't be read.
    Stored taxes are never matched by name, so a tax renamed or changed in settings since is never
    charged next to the one the document holds. The editor mirrors this live
    (_celerpApplyDefaultTaxes in ui/routes/documents.py)."""
    if doc and tax_entries(doc.get("doc_taxes")):
        lines = [line for line in doc.get("line_items") or [] if isinstance(line, dict)]
        carried = [None if line_tax_unset(line) else tax_entries(line.get("taxes"))
                   or legacy_line_taxes(line, taxes, doc_type) for line in lines]
        if not carried or None in carried or len({line_tax_key(each) for each in carried}) != 1:
            return None
        return [{**tax, "amount": 0} for tax in carried[0]]
    return [line_tax(tax) for tax in taxes_for(taxes, doc_type)
            if tax.get("is_default") and float(tax.get("rate") or 0)]


def matching_tax(taxes: list[dict], code: str, rate: float) -> dict | None:
    """The company tax a line charging *rate* under *code* is: the first in *taxes* at that rate
    named *code*, or with no code the first at that rate when it is not zero."""
    return next((tax for tax in taxes if float(tax.get("rate") or 0) == rate
                 and (tax.get("name", "") == code if code else rate != 0)), None)


def legacy_line_taxes(line: dict, taxes: list[dict], doc_type: str | None) -> list[dict] | None:
    """The taxes a line with no taxes list charges through its ``tax_rate`` (and ``tax_code``),
    as an older version and a CSV import with tax columns store them, and as the editor shows
    them: the company tax it matches (matching_tax) as line_tax builds it, else its own code and
    rate. No tax at a zero rate; None when the rate can't be read."""
    try:
        rate = float(line.get("tax_rate") or 0)
    except (TypeError, ValueError):
        return None
    if not rate:
        return []
    code = line.get("tax_code") or ""
    match = matching_tax(taxes_for(taxes, doc_type), code, rate)
    if match is not None:
        return [line_tax(match)]
    return [{"code": code, "rate": rate, "amount": 0, "order": 0, "is_compound": False, "label": ""}]


def set_new_line_taxes(line: dict, taxes: list[dict] | None) -> None:
    """Give a new line the taxes default_line_taxes chose: each its own copy, or None (stored as
    it is) when the user sets the line's tax. A line with no taxes to start with is left as built."""
    if taxes is None:
        line["taxes"] = None
    elif taxes:
        line["taxes"] = [dict(tax) for tax in taxes]


def line_tax_unset(line: dict) -> bool:
    """Whether a line's tax is still for the user to set: its ``taxes`` is stored as None
    (set_new_line_taxes). It charges nothing until then, and the editor shows it as "--"."""
    return "taxes" in line and line["taxes"] is None


def line_tax(tax: dict) -> dict:
    """A company tax as a line carries it: its name, rate, saved calculation order and whether
    it is compound, with no amount yet."""
    return {"code": tax.get("name"), "rate": float(tax["rate"]), "amount": 0,
            "order": int(tax.get("default_order") or 0), "is_compound": bool(tax.get("is_compound")),
            "label": ""}


def line_tax_key(value) -> tuple:
    """What a line's ``taxes`` value charges, for telling whether two lines carry the same taxes:
    each tax's code, rate, order, compounding and label, amounts aside."""
    return tuple((tax.get("code"), float(tax.get("rate") or 0), int(tax.get("order") or 0),
                  bool(tax.get("is_compound")), tax.get("label") or "") for tax in tax_entries(value))


def tax_entries(value) -> list[dict]:
    """The taxes a stored ``doc_taxes`` or line ``taxes`` value holds: its tax objects when it
    is a list, else none. Every write refuses another shape, so one stored by an older version
    shows as no taxes on a page or printout rather than failing it; every path that recalculates
    or rewrites money refuses it instead (unreadable_tax)."""
    return [tax for tax in value if isinstance(tax, dict)] if isinstance(value, list) else []


def tax_text(entry) -> str:
    """A stored tax entry as text that names it: a tax object's code or label, anything else
    as it is written, at most 60 characters."""
    if isinstance(entry, dict):
        entry = entry.get("code") or entry.get("label") or entry
    return str(entry)[:60]


def unreadable_tax(value) -> str | None:
    """The first entry of a ``doc_taxes`` or line ``taxes`` value that is not a tax object, as
    tax_text, or the whole value when it is not a list; None when it is absent or a list of
    tax objects."""
    if value is None:
        return None
    if not isinstance(value, list):
        return tax_text(value)
    return next((tax_text(entry) for entry in value if not isinstance(entry, dict)), None)


def tax_name_key(name) -> str:
    """A tax name as a lookup key: the same name in another case or with spaces around it
    is the same tax."""
    return str(name or "").strip().casefold()


def holds_tax(taxes: list[dict], name: str) -> bool:
    """Whether the tax list holds a tax named ``name`` (as tax_name_key)."""
    return any(tax_name_key(x.get("name")) == tax_name_key(name) for x in taxes)


def misplaced_tax(settings: dict, doc_type: str | None, applied: list[dict]) -> str | None:
    """The name of the first tax in ``applied`` (each a ``code`` and ``rate``) a document of
    this type cannot carry, else None. A tax takes its side from the company's tax lists by
    name, ignoring case and surrounding spaces, the document's own side's list deciding a name
    both lists hold. On a sale a negative rate is always refused: it is tax withheld on a
    purchase, and a sale has no output tax to net it against."""
    side = doc_side(doc_type)
    other = sales_taxes(settings) if side == "purchase" else purchasing_taxes(settings)
    sides = {tax_name_key(x.get("name")): x.get("tax_type") or "both"
             for x in [*other, *side_taxes(settings, doc_type)]}
    for tax in applied:
        code = str(tax.get("code") or "").strip()
        try:
            rate = float(tax.get("rate") or 0)
        except (TypeError, ValueError):
            rate = 0.0  # an unreadable rate is refused where the money is computed
        tax_side = "purchase" if side == "sales" and rate < 0 else sides.get(tax_name_key(code), "both")
        if tax_side not in (side, "both"):
            return code or f"{rate:g}%"
    return None


def retired_taxes() -> list[dict]:
    """The rows earlier seeds wrote that are no longer right, as the loaded country packs list them
    (each pack's ``retired``, in lock order; the RetiredTax schema in routers/companies.py). A
    company keeps the taxes it saved, so each entry is told to it as a notice
    (services/tax_seed_notice.py) and never changed for it."""
    return [entry for pack in packs().values() for entry in pack["retired"]]


def _row_matches(row: dict, want: dict) -> bool:
    """Whether a stored tax row is the retired row ``want`` describes."""
    for field, value in want.items():
        if field == "name":
            if tax_name_key(row.get("name")) != tax_name_key(value):
                return False
        elif field == "rate":
            try:
                if float(row.get("rate") or 0) != value:
                    return False
            except (TypeError, ValueError):
                return False
        elif bool(row.get(field)) != value:
            return False
    return True


def retired_seed_rows(settings: dict) -> list[dict]:
    """The retired_taxes entries a company's saved tax lists still hold, in that order, each
    ``{"id", "lists"}`` naming the lists ("sales", "purchase") it was found in. Only lists the
    company saved are read: a purchasing list never saved is a copy of the sales list, not a
    second place to find the same row. An entry of another currency than the saved one is not
    read. A list or row of the wrong shape holds nothing."""
    saved = {"sales": settings.get("taxes"), "purchase": settings.get("purchasing_taxes")}
    lists = {side: [r for r in rows if isinstance(r, dict)]
             for side, rows in saved.items() if isinstance(rows, list)}
    found, books = [], settings.get("currency")
    for entry in retired_taxes():
        if books and entry.get("currency", books) != books:
            continue
        test = all if entry["match"] == "all" else any
        sides = {side for side, rows in lists.items()
                 if test(any(_row_matches(r, want) for r in rows) for want in entry["rows"])}
        if sides:
            found.append({"id": entry["id"], "lists": sides})
    return found
