# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: MIT

"""Which chart accounts can take money, and which changes to the chart would leave a
bank account posting to an account that cannot."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from celerp_accounting.models import Account, BankAccount
from ui.i18n import t


def require_open_account(acc: Account | None, code: str) -> Account:
    """422 unless *acc* (the chart row for *code*) exists and is active."""
    if acc is None:
        raise HTTPException(status_code=422, detail=t("acct.err_account_unknown", code=code))
    if not acc.is_active:
        raise HTTPException(status_code=422, detail=t("acct.err_account_archived", code=code))
    return acc


async def require_money_account(session: AsyncSession, company_id: uuid.UUID, code: str) -> Account:
    """422 unless *code* is one of the company's active asset accounts. The row is read
    FOR SHARE, so it cannot be archived or retyped until the caller's transaction ends."""
    acc = (await session.execute(
        select(Account).where(Account.company_id == company_id, Account.code == code)
        .with_for_update(read=True)
    )).scalar_one_or_none()
    require_open_account(acc, code)
    if acc.account_type != "asset":
        raise HTTPException(status_code=422, detail=t(
            "acct.err_account_not_money", code=code, type=t(f"enum.account_type.{acc.account_type}")))
    return acc


async def check_account_change(
    session: AsyncSession, company_id: uuid.UUID, acc: Account, *,
    account_type: str | None, is_active: bool | None,
) -> None:
    """422 when archiving *acc* or changing its type away from asset would leave an active
    bank account posting to it. The caller holds *acc* FOR UPDATE."""
    archives = is_active is False and acc.is_active
    retypes = account_type is not None and account_type != acc.account_type and acc.account_type == "asset"
    if not (archives or retypes):
        return
    banks = (await session.scalars(
        select(BankAccount.bank_name).where(
            BankAccount.company_id == company_id, BankAccount.chart_account_code == acc.code,
            BankAccount.is_active.is_(True)).order_by(BankAccount.bank_name)
    )).all()
    if banks:
        raise HTTPException(status_code=422, detail=(
            f"Account {acc.code} belongs to the active bank account {', '.join(banks)}. Archive the bank "
            "account before archiving this account or changing its type."))
