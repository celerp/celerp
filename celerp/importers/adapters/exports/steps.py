# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Reading steps every source shares once its files are in a `Book`: the base currency,
foreign bank accounts, contact roles, tax rates, the trial balances rolled up from the
first record, and the aged balances' date checked against the ledger."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from celerp.importers.adapters.base import ScanError
from celerp.importers.adapters.exports.book import PROFIT_AND_LOSS, ZERO, Book
from ui.i18n import t


def show(on: date) -> str:
    return f"{on.day} {on:%b %Y}"


def month_end(on: date) -> date:
    following = date(on.year + on.month // 12, on.month % 12 + 1, 1)
    return date.fromordinal(following.toordinal() - 1)


def base_currency(book: Book) -> str:
    """The currency of the journals that state no foreign amount on any line."""
    found = Counter(j.currency for j in book.journals if j.currency and all(ln.foreign is None for ln in j.lines))
    if not found:
        raise ScanError(t("migration.err_export_no_base"))
    return found.most_common(1)[0][0]


def bank_currencies(book: Book) -> None:
    """A bank account is kept in a foreign currency when every line on it states that currency."""
    seen: dict[str, set[str]] = defaultdict(set)
    for j in book.journals:
        for ln in j.lines:
            if book.accounts[ln.account].control == "bank":
                seen[ln.account].add(j.currency if ln.foreign is not None and j.currency != book.base else book.base)
    for code, currencies in seen.items():
        if len(currencies) == 1 and book.is_foreign(next(iter(currencies))):
            book.accounts[code].currency = next(iter(currencies))


def roles_from_ledger(book: Book) -> None:
    receivable, payable = set(book.control("receivable")), set(book.control("payable"))
    for j in book.journals:
        for ln in j.lines:
            contact = ln.contact or j.contact
            if contact and ln.account in receivable:
                book.contact(contact).roles.add("customer")
            elif contact and ln.account in payable:
                book.contact(contact).roles.add("supplier")


def tax_rates(book: Book) -> None:
    """A tax code's rate from a leading percentage in its name, otherwise from the lines that use it."""
    for code in book.tax_codes.values():
        head = code.name.split("%", 1)[0].strip()
        if "%" in code.name and head.replace(".", "", 1).isdigit():
            code.rate = Decimal(head)
        else:
            rates = {(ln.tax * 100 / ln.net).quantize(Decimal("0.1")) for d in book.documents.values()
                     for ln in d.lines if ln.tax_code == code.name and ln.net}
            code.rate = rates.pop() if len(rates) == 1 else ZERO
        if code.rate:
            if book.tax_account is None:
                raise ScanError(t("migration.err_export_tax_account", tax=code.name))
            code.account = book.tax_account


def roll_up_trial_balances(book: Book, years: list[dict[date, dict[str, Decimal]]]) -> None:
    """Balances from the first record at each date a trial balance states. Each year's
    trial balances state profit and loss for that financial year so far and carry earlier
    years' profit in retained earnings."""
    earlier: dict[str, Decimal] = defaultdict(lambda: ZERO)
    retained = book.control("retained_earnings")
    for year in sorted(years, key=min):
        for on, balances in sorted(year.items()):
            out = dict(balances)
            for code, carried in earlier.items():
                out[code] = out.get(code, ZERO) + carried
            profit = sum(earlier.values(), ZERO)
            for code in retained[:1]:
                out[code] = out.get(code, ZERO) - profit
            book.trial_balances[on] = out
        for code, value in year[max(year)].items():
            if book.accounts[code].account_type in PROFIT_AND_LOSS:
                earlier[code] += value


def refuse_stale_aged(book: Book, label: str, as_at: date) -> None:
    """An aged report must state the balances as at the last ledger date or later."""
    last = book.journals[-1].date if book.journals else None
    if last and as_at < last:
        raise ScanError(t("migration.err_export_aged_stale", label=label, as_at=show(as_at), last=show(last)))
