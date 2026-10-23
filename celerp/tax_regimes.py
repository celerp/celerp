# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""
Country-based tax regime defaults.

Keyed by ISO 3166-1 alpha-2 country code (uppercase).
Used to seed company.settings["taxes"] and company.settings["currency"]
when a default location with a known country is first set.

Each entry:
  currency: str          ISO 4217 code
  taxes: list[dict]      matches the TaxRate schema in routers/companies.py

"_default" is the fallback for unknown/unset country.
"""
from __future__ import annotations

import copy

from celerp.services.list_behavior import SUPPLIER_DOC_TYPES

_T = dict  # type alias for readability

TAX_REGIMES: dict[str, _T] = {
    # ── Southeast Asia ────────────────────────────────────────────────────
    "TH": {
        "currency": "THB",
        "taxes": [
            {"name": "VAT 7%", "rate": 7.0, "tax_type": "both", "is_default": True,
             "description": "Thailand standard VAT", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
            {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False,
             "description": "Withholding tax on services", "is_compound": False, "default_order": 1},
            {"name": "WHT 5%", "rate": -5.0, "tax_type": "purchase", "is_default": False,
             "description": "Withholding tax on rentals", "is_compound": False, "default_order": 1},
        ],
    },
    "SG": {
        "currency": "SGD",
        "taxes": [
            {"name": "GST 9%", "rate": 9.0, "tax_type": "both", "is_default": True,
             "description": "Singapore GST", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "GST-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "MY": {
        "currency": "MYR",
        "taxes": [
            {"name": "Sales Tax 10%", "rate": 10.0, "tax_type": "sales", "is_default": True,
             "description": "Malaysia sales tax, standard rate", "is_compound": False, "default_order": 0},
            {"name": "Sales Tax 5%", "rate": 5.0, "tax_type": "sales", "is_default": False,
             "description": "Malaysia sales tax, reduced rate for listed goods",
             "is_compound": False, "default_order": 0},
            {"name": "Service Tax 8%", "rate": 8.0, "tax_type": "sales", "is_default": False,
             "description": "Malaysia service tax (selected services)", "is_compound": False, "default_order": 0},
            {"name": "Service Tax 6%", "rate": 6.0, "tax_type": "sales", "is_default": False,
             "description": "Malaysia service tax on listed services such as food and drink, telecoms, "
                            "logistics, parking and rentals",
             "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "PH": {
        "currency": "PHP",
        "taxes": [
            {"name": "VAT 12%", "rate": 12.0, "tax_type": "both", "is_default": True,
             "description": "Philippines VAT", "is_compound": False, "default_order": 0},
            {"name": "Zero-rated", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Zero-rated VAT (exports)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
            {"name": "EWT 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False,
             "description": "Expanded withholding tax", "is_compound": False, "default_order": 1},
        ],
    },
    "ID": {
        "currency": "IDR",
        "taxes": [
            {"name": "PPN 11%", "rate": 11.0, "tax_type": "both", "is_default": True,
             "description": "12% on a base of 11/12 of the price, for goods that are not luxury goods",
             "is_compound": False, "default_order": 0},
            {"name": "PPN 12%", "rate": 12.0, "tax_type": "both", "is_default": False,
             "description": "Luxury goods, on the full price", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "PPN-exempt", "is_compound": False, "default_order": 0},
            {"name": "PPh 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False,
             "description": "Income tax withholding (PPh 23)", "is_compound": False, "default_order": 1},
        ],
    },
    "VN": {
        "currency": "VND",
        "taxes": [
            {"name": "VAT 10%", "rate": 10.0, "tax_type": "both", "is_default": True,
             "description": "Vietnam standard VAT", "is_compound": False, "default_order": 0},
            {"name": "VAT 8%", "rate": 8.0, "tax_type": "both", "is_default": False,
             "description": "Reduced rate until 31 Dec 2026. Excluded sectors stay at 10%",
             "is_compound": False, "default_order": 0},
            {"name": "VAT 5%", "rate": 5.0, "tax_type": "both", "is_default": False,
             "description": "Vietnam reduced VAT (essential goods)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Oceania ───────────────────────────────────────────────────────────
    "AU": {
        "currency": "AUD",
        "taxes": [
            {"name": "GST 10%", "rate": 10.0, "tax_type": "both", "is_default": True,
             "description": "Australia GST", "is_compound": False, "default_order": 0},
            {"name": "GST-free", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "GST-free supply", "is_compound": False, "default_order": 0},
        ],
    },
    "NZ": {
        "currency": "NZD",
        "taxes": [
            {"name": "GST 15%", "rate": 15.0, "tax_type": "both", "is_default": True,
             "description": "New Zealand GST", "is_compound": False, "default_order": 0},
            {"name": "Zero-rated", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Zero-rated GST (exports)", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Europe ────────────────────────────────────────────────────────────
    "GB": {
        "currency": "GBP",
        "taxes": [
            {"name": "VAT 20%", "rate": 20.0, "tax_type": "both", "is_default": True,
             "description": "UK standard VAT", "is_compound": False, "default_order": 0},
            {"name": "VAT 5%", "rate": 5.0, "tax_type": "both", "is_default": False,
             "description": "UK reduced rate VAT", "is_compound": False, "default_order": 0},
            {"name": "Zero-rated", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Zero-rated VAT", "is_compound": False, "default_order": 0},
        ],
    },
    "DE": {
        "currency": "EUR",
        "taxes": [
            {"name": "MwSt 19%", "rate": 19.0, "tax_type": "both", "is_default": True,
             "description": "Germany standard VAT", "is_compound": False, "default_order": 0},
            {"name": "MwSt 7%", "rate": 7.0, "tax_type": "both", "is_default": False,
             "description": "Germany reduced VAT", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "FR": {
        "currency": "EUR",
        "taxes": [
            {"name": "TVA 20%", "rate": 20.0, "tax_type": "both", "is_default": True,
             "description": "France standard VAT", "is_compound": False, "default_order": 0},
            {"name": "TVA 10%", "rate": 10.0, "tax_type": "both", "is_default": False,
             "description": "France intermediate rate", "is_compound": False, "default_order": 0},
            {"name": "TVA 5.5%", "rate": 5.5, "tax_type": "both", "is_default": False,
             "description": "France reduced rate", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── North America ─────────────────────────────────────────────────────
    "US": {
        "currency": "USD",
        "taxes": [
            # No federal VAT; state/local sales taxes vary. User must configure.
            {"name": "Sales Tax", "rate": 0.0, "tax_type": "sales", "is_default": True,
             "description": "State and local sales tax. Set the rate for your area",
             "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    "CA": {
        "currency": "CAD",
        "taxes": [
            {"name": "GST 5%", "rate": 5.0, "tax_type": "both", "is_default": True,
             "description": "Canada federal GST", "is_compound": False, "default_order": 0},
            {"name": "PST", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Provincial sales tax. Set the rate for your province",
             "is_compound": False, "default_order": 1},
            {"name": "QST 9.975%", "rate": 9.975, "tax_type": "both", "is_default": False,
             "description": "Quebec sales tax, charged on the price before GST",
             "is_compound": False, "default_order": 1},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Asia ──────────────────────────────────────────────────────────────
    "IN": {
        "currency": "INR",
        "taxes": [
            {"name": "CGST 9%", "rate": 9.0, "tax_type": "both", "is_default": False,
             "description": "India central GST (standard rate split)", "is_compound": False, "default_order": 0},
            {"name": "SGST 9%", "rate": 9.0, "tax_type": "both", "is_default": False,
             "description": "India state GST (standard rate split)", "is_compound": False, "default_order": 0},
            {"name": "IGST 18%", "rate": 18.0, "tax_type": "both", "is_default": True,
             "description": "Integrated GST on sales between states. Within a state, 18% is charged as "
                            "CGST 9% plus SGST 9%", "is_compound": False, "default_order": 0},
            {"name": "GST 5%", "rate": 5.0, "tax_type": "both", "is_default": False,
             "description": "India reduced GST rate", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "GST-exempt", "is_compound": False, "default_order": 0},
            {"name": "TDS 2%", "rate": -2.0, "tax_type": "purchase", "is_default": False,
             "description": "Tax deducted at source", "is_compound": False, "default_order": 1},
        ],
    },
    "JP": {
        "currency": "JPY",
        "taxes": [
            {"name": "消費税 10%", "rate": 10.0, "tax_type": "both", "is_default": True,
             "description": "Japan consumption tax (standard)", "is_compound": False, "default_order": 0},
            {"name": "消費税 8%", "rate": 8.0, "tax_type": "both", "is_default": False,
             "description": "Japan consumption tax (reduced rate for food and drink)", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
    # ── Africa ────────────────────────────────────────────────────────────
    "ET": {
        "currency": "ETB",
        "taxes": [
            {"name": "VAT 15%", "rate": 15.0, "tax_type": "both", "is_default": True,
             "description": "Ethiopia standard VAT", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "VAT-exempt", "is_compound": False, "default_order": 0},
            {"name": "WHT 3%", "rate": -3.0, "tax_type": "purchase", "is_default": False,
             "description": "Withholding on goods contracts above ETB 20,000 and services from ETB 10,000. "
                            "30% when the supplier gives no TIN", "is_compound": False, "default_order": 1},
        ],
    },
    # ── Fallback ──────────────────────────────────────────────────────────
    "_default": {
        "currency": "USD",
        "taxes": [
            {"name": "Standard Tax", "rate": 0.0, "tax_type": "both", "is_default": True,
             "description": "Set your local tax rate", "is_compound": False, "default_order": 0},
            {"name": "Exempt", "rate": 0.0, "tax_type": "both", "is_default": False,
             "description": "Tax-exempt", "is_compound": False, "default_order": 0},
        ],
    },
}


def get_regime(country_code: str | None) -> dict:
    """Return the tax regime for a country code (case-insensitive). Falls back to _default."""
    if not country_code:
        return TAX_REGIMES["_default"]
    return TAX_REGIMES.get(country_code.strip().upper(), TAX_REGIMES["_default"])


def generic_taxes() -> list[dict]:
    """The neutral tax list a company starts from until it configures its own."""
    return copy.deepcopy(TAX_REGIMES["_default"]["taxes"])


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
    a company default. When they differ or there are none, None: the line carries no tax of its
    own until the user sets one (the editor shows "--"). Nothing is matched by name, so a tax
    renamed or changed in settings since is never charged next to the one the document holds.
    The editor mirrors this live (_celerpApplyDefaultTaxes in ui/routes/documents.py)."""
    if doc and tax_entries(doc.get("doc_taxes")):
        lines = [line for line in doc.get("line_items") or [] if isinstance(line, dict)]
        if len({line_tax_key(line.get("taxes")) for line in lines}) != 1:
            return None
        return [{**tax, "amount": 0} for tax in tax_entries(lines[0].get("taxes"))]
    return [line_tax(tax) for tax in taxes_for(taxes, doc_type)
            if tax.get("is_default") and float(tax.get("rate") or 0)]


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


def _tax_name_key(name) -> str:
    """A tax name as a lookup key: the same name in another case or with spaces around it
    is the same tax."""
    return str(name or "").strip().casefold()


def holds_tax(taxes: list[dict], name: str) -> bool:
    """Whether the tax list holds a tax named ``name`` (as _tax_name_key)."""
    return any(_tax_name_key(x.get("name")) == _tax_name_key(name) for x in taxes)


def misplaced_tax(settings: dict, doc_type: str | None, applied: list[dict]) -> str | None:
    """The name of the first tax in ``applied`` (each a ``code`` and ``rate``) a document of
    this type cannot carry, else None. A tax takes its side from the company's tax lists by
    name, ignoring case and surrounding spaces, the document's own side's list deciding a name
    both lists hold. On a sale a negative rate is always refused: it is tax withheld on a
    purchase, and a sale has no output tax to net it against."""
    side = doc_side(doc_type)
    other = sales_taxes(settings) if side == "purchase" else purchasing_taxes(settings)
    sides = {_tax_name_key(x.get("name")): x.get("tax_type") or "both"
             for x in [*other, *side_taxes(settings, doc_type)]}
    for tax in applied:
        code = str(tax.get("code") or "").strip()
        try:
            rate = float(tax.get("rate") or 0)
        except (TypeError, ValueError):
            rate = 0.0  # an unreadable rate is refused where the money is computed
        tax_side = "purchase" if side == "sales" and rate < 0 else sides.get(_tax_name_key(code), "both")
        if tax_side not in (side, "both"):
            return code or f"{rate:g}%"
    return None


# Rows earlier seeds wrote that are no longer right. A company keeps the taxes it saved, so each
# entry is told to the company as a notice (services/tax_seed_notice.py) and never changed for it.
# An entry matches a tax list holding a row for each of its ``rows`` ("all") or for any one of them
# ("any"); a row matches on its name (as _tax_name_key) and every other field given.
RETIRED_SEED_ROWS: tuple[dict, ...] = (
    {"id": "in-default-2025", "match": "all",
     "rows": ({"name": "CGST 9%", "is_default": True}, {"name": "SGST 9%", "is_default": True})},
    {"id": "et-wht-2025", "match": "any", "rows": ({"name": "WHT 2%", "rate": -2.0},)},
    {"id": "et-tot-2025", "match": "any", "rows": ({"name": "TOT 2%"}, {"name": "TOT 10%"})},
    {"id": "ca-qst-compound", "match": "any", "rows": ({"name": "QST 9.975%", "is_compound": True},)},
    {"id": "my-sst-6", "match": "any", "rows": ({"name": "SST 6%", "rate": 6.0},)},
)


def _row_matches(row: dict, want: dict) -> bool:
    """Whether a stored tax row is the retired row ``want`` describes."""
    for field, value in want.items():
        if field == "name":
            if _tax_name_key(row.get("name")) != _tax_name_key(value):
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
    """The RETIRED_SEED_ROWS entries a company's saved tax lists still hold, in that order, each
    ``{"id", "lists"}`` naming the lists ("sales", "purchase") it was found in. Only lists the
    company saved are read: a purchasing list never saved is a copy of the sales list, not a
    second place to find the same row. A list or row of the wrong shape holds nothing."""
    saved = {"sales": settings.get("taxes"), "purchase": settings.get("purchasing_taxes")}
    lists = {side: [r for r in rows if isinstance(r, dict)]
             for side, rows in saved.items() if isinstance(rows, list)}
    found = []
    for entry in RETIRED_SEED_ROWS:
        test = all if entry["match"] == "all" else any
        sides = {side for side, rows in lists.items()
                 if test(any(_row_matches(r, want) for r in rows) for want in entry["rows"])}
        if sides:
            found.append({"id": entry["id"], "lists": sides})
    return found
