# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1

"""Setting a password, the one place a password hash is written.

A self-service change (`set_password`) and a reset (`reset_password`: by a code the
relay emailed, the terminal command or an owner) both end every session the user
had. A reset is also recorded, and the user, the installation owner and the owners
of the user's companies are told about it at their next sign-in.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import celerp
from celerp.config import config_path, settings
from celerp.models.accounting import UserCompany
from celerp.models.auth import PasswordReset, PasswordResetNotice
from celerp.models.company import User
from celerp.services.auth import hash_password, installation_root_user_id, validate_password
from celerp.services.session_tracker import invalidate_sessions


async def user_by_email(session: AsyncSession, email: str) -> User | None:
    """The active user whose email is exactly *email*: the one sign-in matches."""
    return (await session.execute(
        select(User).where(User.email == email, User.is_active.is_(True)))).scalar_one_or_none()


async def set_password(session: AsyncSession, user: User, new_password: str) -> None:
    """Give *user* *new_password* and end every session they had; commits.

    Raises ValueError when the password is below policy, before anything is written."""
    validate_password(new_password)
    user.auth_hash = hash_password(new_password)
    await invalidate_sessions(session, str(user.id))


async def _told_of_reset(session: AsyncSession, user: User) -> list:
    """The user, the installation owner and every active owner of a company the user
    belongs to, each once."""
    companies = select(UserCompany.company_id).where(
        UserCompany.user_id == user.id, UserCompany.is_active.is_(True))
    owners = (await session.execute(
        select(UserCompany.user_id).where(
            UserCompany.company_id.in_(companies), UserCompany.role == "owner",
            UserCompany.is_active.is_(True)).order_by(UserCompany.user_id))).scalars()
    install_owner = await installation_root_user_id(session)
    return list(dict.fromkeys([user.id, *([install_owner] if install_owner else []), *owners]))


async def reset_password(
    session: AsyncSession, user: User, new_password: str, *,
    method: str, os_user: str | None = None, by_user: User | None = None,
) -> None:
    """Reset *user*'s password by *method*, record it and leave a notice for everyone
    told of it except *by_user*; all of it commits together or not at all."""
    validate_password(new_password)
    reset = PasswordReset(user_id=user.id, method=method, os_user=os_user,
                          by_user_id=by_user.id if by_user else None)
    session.add(reset)
    await session.flush()
    for recipient_id in await _told_of_reset(session, user):
        if by_user is None or recipient_id != by_user.id:
            session.add(PasswordResetNotice(reset_id=reset.id, recipient_id=recipient_id))
    await session.flush()
    await set_password(session, user, new_password)


def _posix_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _powershell_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def terminal_command(*, windows: bool | None = None) -> str:
    """The command that resets a password from a terminal on this computer, built from
    this process's own interpreter, app folder, data folder and config file, so the
    terminal reaches this install and no other. `-E` keeps the terminal's own Python
    variables out."""
    if windows is None:
        windows = os.name == "nt"
    app_dir = str(Path(celerp.__file__).resolve().parent.parent)
    env = {"CELERP_DATA_DIR": str(settings.data_dir), "CELERP_CONFIG": str(config_path())}
    run = "-E -X utf8 -m celerp reset-password"
    if windows:
        q = _powershell_quote
        exports = "".join(f"$env:{k}={q(v)}; " for k, v in env.items())
        return f"Set-Location -LiteralPath {q(app_dir)}; {exports}& {q(sys.executable)} {run}"
    q = _posix_quote
    exports = "".join(f"{k}={q(v)} " for k, v in env.items())
    return f"cd {q(app_dir)} && {exports}{q(sys.executable)} {run}"
