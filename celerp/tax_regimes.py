# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Country-based tax regime defaults.

Each country's regime comes from the release's country packs (``celerp.localization``), keyed by
ISO 3166-1 alpha-2 country code (uppercase); a company takes one by Update localization.

Each regime:
  currency: str          ISO 4217 code
  taxes: list[dict]      the pack's tax codes (celerp.localization.taxcodes.TaxCode dumps)

``DEFAULT_REGIME`` is the fallback for an unknown or unset country.
"""
from __future__ import annotations

import copy
import datetime as dt

from celerp.localization import lock_codes, missing, packs
from celerp.services.list_behavior import SUPPLIER_DOC_TYPES

# The sides a tax is for; a row stored without one, or with a blank one, is for both.
TAX_TYPES = ("sales", "purchase", "both")
# The settings page that edits each side's tax list.
TAX_LIST_PAGES = {"sales": "/settings/sales?tab=taxes", "purchase": "/settings/purchasing?tab=taxes"}

DEFAULT_REGIME: dict = {
    "currency": "USD",
    "taxes": [
        {"name": "Standard Tax", "versions": [{"rate": 0.0}], "tax_type": "both", "is_default": True,
         "description": "Set your local tax rate", "is_compound": False, "default_order": 0},
        {"name": "Exempt", "versions": [{"rate": 0.0}], "tax_type": "both", "is_default": False,
         "description": "Tax-exempt", "is_compound": False, "default_order": 0},
    ],
}


def tax_countries() -> tuple[str, ...]:
    """The countries a company can name to start with the tax rates in force there: the lock's
    countries whose pack loaded, in lock order."""
    return tuple(packs())


def _code(value: str | None) -> str:
    """``value`` as a pack code: upper case, a test pack's ``zz`` prefix kept as written."""
    code = str(value or "").strip()
    return "zz" + code[2:].upper() if code[:2].lower() == "zz" else code.upper()


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


def country_refusal(value: str | None) -> str | None:
    """Why ``value`` cannot be the company's country, else None (no country is a choice too:
    Other country)."""
    if refusal := missing_pack_refusal(value):
        return refusal
    if _code(value) and not tax_country(value):
        from ui.i18n import t
        return t("setup.invalid_country", value=repr(_code(value)))
    return None


def get_regime(country_code: str | None) -> dict:
    """Return the tax regime for a country code (case-insensitive). Falls back to DEFAULT_REGIME."""
    return packs().get(_code(country_code), DEFAULT_REGIME)


# Categories the buyer accounts for itself: the tax is computed on the line and posted both as
# output and input on a purchase, but never charged, so it is in no tax or total.
SELF_ASSESSED = ("reverse_charge", "self_assessed_import")


def charged(tax) -> bool:
    """Whether a tax snapshot (a LineTaxSnapshot or its stored dict) is charged on the document:
    every tax but a self-assessed one (SELF_ASSESSED), which the buyer accounts for itself."""
    category = tax.get("category") if isinstance(tax, dict) else tax.category
    return category not in SELF_ASSESSED

# Categories whose tax is settled when the document is, never when the cash moves (V36).
_NOT_CASH_BASIS = ("withholding", "perception", *SELF_ASSESSED)


def with_taxes(settings: dict, side: str, taxes: list[dict]) -> dict:
    """``settings`` with the company's ``side`` ("sales" or "purchase") tax list set to ``taxes``
    as normalized_list stores it. Cash basis codes are refused (TaxListRefused, naming every such
    code) on a category settled with the document, and when the pack of the localization applied
    has no cash basis cut-off."""
    from celerp.localization.taxcodes import TaxListRefused, normalized_list
    stored = normalized_list(taxes)
    applied = (settings.get("localization") or {}).get("country")
    cutoff = applied and packs().get(applied, {}).get("cash_basis_cutoff")
    cash_basis = [code for code in stored if code["cash_basis"]]
    wrong_kind = [code["name"] for code in cash_basis if code["category"] in _NOT_CASH_BASIS]
    if wrong_kind:
        raise TaxListRefused("company.err_tax_cash_basis_category", names=", ".join(wrong_kind))
    if cash_basis and not cutoff:
        raise TaxListRefused("company.err_tax_cash_basis_cutoff", names=", ".join(code["name"] for code in cash_basis))
    return {**settings, "purchasing_taxes" if side == "purchase" else "taxes": stored}


def tax_day(doc: dict | None, timezone_name: str | None = None) -> dt.date:
    """The day a document's taxes are charged on: its issue date, else today in the business
    timezone (a new document, or one with no date yet)."""
    from celerp.services.business_time import business_date_of
    return dt.date.fromisoformat(business_date_of((doc or {}).get("issue_date"), timezone_name))


def taxes_on(taxes: list[dict], day: dt.date) -> list[dict]:
    """A stored tax list as it charges on ``day``: each code in force that day with its ``rate``
    as a line carries it (_line_rate); a code with no version in force that day is left out.
    Every reader of a list row's rate reads it here."""
    from celerp.localization.taxcodes import NoRateInForce
    rows = []
    for row in taxes:
        try:
            rate = _line_rate(_tax_code(row), day)
        except NoRateInForce:
            continue
        rows.append({**row, "rate": rate})
    return rows


def _tax_code(row: dict):
    """A stored tax list row as a TaxCode. A row of taxes_on also carries its ``rate`` on the day;
    the rate is read from its versions, never from that copy."""
    from celerp.localization.taxcodes import TaxCode
    return TaxCode.model_validate({key: value for key, value in row.items() if key != "rate"})


def _line_rate(code, day: dt.date) -> float:
    """The rate of ``code`` on ``day`` (rate_on) as a line carries it: a withholding code's
    negative."""
    from celerp.localization.taxcodes import rate_on
    rate = rate_on(code, day)
    return -rate if code.category == "withholding" else rate


def outdated_line_tax(tax: dict, codes: dict[str, dict], day: dt.date) -> bool:
    """Whether a line tax built from a company code (``codes`` by id, rows of the document's
    side) holds a version other than the one in force on ``day``: the document's date moved
    past a rate change, or to a day the code has no rate. A tax with no code id, or whose code
    is gone, is not."""
    from celerp.localization.taxcodes import NoRateInForce, version_on
    row = codes.get(tax.get("code_id") or "")
    if row is None:
        return False
    code = _tax_code(row)
    try:
        start = code.versions[version_on(code, day)].valid_from
    except NoRateInForce:
        return True
    return (start.isoformat() if start else None) != tax.get("version_from")


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


def default_line_taxes(taxes: list[dict], doc_type: str | None, day: dt.date, *,
                       doc: dict | None) -> list[dict] | None:
    """The taxes list a new line starts with, whichever way it is added (scan, from items, append,
    reorder, the editor). `doc` is the target document's stored state, None for a new one; `day`
    is the day its taxes are charged on (tax_day).

    On a document with no document tax: every company default for the document's side with a
    rate in force on `day` that is not zero, in list order (a company may hold two, such as CGST
    9% and SGST 9%), each as line_tax builds it on `day`; a default with no rate that day is left
    out. On a draft that already charges a tax as a whole, its taxation
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
                   or legacy_line_taxes(line, taxes, doc_type, day) for line in lines]
        if not carried or None in carried or len({line_tax_key(each) for each in carried}) != 1:
            return None
        return [{**tax, "amount": 0} for tax in carried[0]]
    side = doc_side(doc_type)
    return [line_tax(tax, day, side) for tax in taxes_on(taxes_for(taxes, doc_type), day)
            if tax.get("is_default") and tax["rate"]]


def item_line_taxes(item: dict, taxes: list[dict], doc_type: str | None, day: dt.date, *,
                    doc: dict | None) -> list[dict] | None:
    """The taxes a new line for catalog *item* (its state) starts with: the codes its
    ``tax_codes`` names on the document's side with a rate in force on *day*, each as line_tax
    builds it, in the item's order; when it names none (or none of them is in force that day),
    default_line_taxes."""
    by_id = {tax.get("id"): tax for tax in taxes_on(taxes_for(taxes, doc_type), day)}
    side = doc_side(doc_type)
    own = [line_tax(by_id[code_id], day, side) for code_id in item.get("tax_codes") or [] if code_id in by_id]
    return own or default_line_taxes(taxes, doc_type, day, doc=doc)


def matching_tax(taxes: list[dict], code: str, rate: float, day: dt.date) -> dict | None:
    """The company tax a line charging *rate* under *code* is: the first in *taxes* whose rate in
    force on *day* is *rate* named *code*, or with no code the first at that rate when it is not
    zero."""
    return next((tax for tax in taxes_on(taxes, day) if tax["rate"] == rate
                 and (tax.get("name", "") == code if code else rate != 0)), None)


def legacy_line_taxes(line: dict, taxes: list[dict], doc_type: str | None, day: dt.date) -> list[dict] | None:
    """The taxes a line with no taxes list charges through its ``tax_rate`` (and ``tax_code``),
    as an older version and a CSV import with tax columns store them, and as the editor shows
    them: the company tax it matches on ``day`` (matching_tax) as line_tax builds it on that
    day, else its own code and rate. No tax at a zero rate; None when the rate can't be read."""
    try:
        rate = float(line.get("tax_rate") or 0)
    except (TypeError, ValueError):
        return None
    if not rate:
        return []
    code = line.get("tax_code") or ""
    match = matching_tax(taxes_for(taxes, doc_type), code, rate, day)
    if match is not None:
        return [line_tax(match, day, doc_side(doc_type))]
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


def line_tax(code: dict, day: dt.date, side: str) -> dict:
    """A company tax code as a line on a ``side`` ("sales" or "purchase") document carries it on
    ``day`` (LineTaxSnapshot): its name as ``code``, its id, the version in force that day (its
    rate as _line_rate gives it, start and base fraction), its category, component, report tags,
    withholding point, cash basis and the account of that side (None posts to the role default),
    its saved calculation order and compounding, with no amount yet. NoRateInForce when no
    version is in force that day. Posting reads this copy, never the live code."""
    from celerp.localization.taxcodes import LineTaxSnapshot, version_on
    tax = _tax_code(code)
    version = tax.versions[version_on(tax, day)]
    return LineTaxSnapshot(
        code=tax.name, rate=_line_rate(tax, day), order=tax.default_order, is_compound=tax.is_compound,
        code_id=tax.id, base_fraction=version.base_fraction, category=tax.category, component=tax.component,
        report_tags=tax.report_tags, version_from=version.valid_from, withholding_point=tax.withholding_point,
        cash_basis=tax.cash_basis, account=tax.input_account if side == "purchase" else tax.output_account,
    ).model_dump(mode="json")


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
    (each pack's ``retired``, in lock order; the RetiredTax schema in routers/companies.py), each
    with its pack's ``country``: an id is unique within one pack only, so an entry is known by its
    country and id together. A company keeps the taxes it saved, so each entry is told to it as a
    notice (services/tax_seed_notice.py) and never changed for it."""
    return [{**entry, "country": code} for code, pack in packs().items() for entry in pack["retired"]]


def _row_matches(row: dict, want: dict, day: dt.date) -> bool:
    """Whether a stored tax row is the retired row ``want`` describes on ``day``: its rate is the
    one in force that day (rate_on); a row that is not a valid tax code, or has no rate in force,
    matches no rate."""
    from celerp.localization.taxcodes import TaxCode, rate_on
    for field, value in want.items():
        if field == "name":
            if tax_name_key(row.get("name")) != tax_name_key(value):
                return False
        elif field == "rate":
            try:
                if rate_on(TaxCode.model_validate(row), day) != value:
                    return False
            except ValueError:  # not a valid code (pydantic's ValidationError), or NoRateInForce
                return False
        elif field == "category":
            if row.get("category") != value:
                return False
        elif bool(row.get(field)) != value:
            return False
    return True


def retired_seed_rows(settings: dict) -> list[dict]:
    """The retired_taxes entries a company's saved tax lists still hold, in that order, each
    ``{"country", "id", "lists"}`` naming the lists ("sales", "purchase") it was found in. Only lists the
    company saved are read: a purchasing list never saved is a copy of the sales list, not a
    second place to find the same row. An entry of another currency than the saved one is not
    read. A list or row of the wrong shape holds nothing. Rates are compared as they stand today
    in the business timezone."""
    day = tax_day(None, settings.get("timezone"))
    saved = {"sales": settings.get("taxes"), "purchase": settings.get("purchasing_taxes")}
    lists = {side: [r for r in rows if isinstance(r, dict)]
             for side, rows in saved.items() if isinstance(rows, list)}
    found, books = [], settings.get("currency")
    for entry in retired_taxes():
        if books and entry.get("currency", books) != books:
            continue
        test = all if entry["match"] == "all" else any
        sides = {side for side, rows in lists.items()
                 if test(any(_row_matches(r, want, day) for r in rows) for want in entry["rows"])}
        if sides:
            found.append({"country": entry["country"], "id": entry["id"], "lists": sides})
    return found
