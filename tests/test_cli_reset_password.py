# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""`celerp reset-password`, the command the recovery page gives the person at this
computer: it resets through the same function as every other reset, finds the
desktop's database from the address the app publishes, and says plainly when it
cannot reach it."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from click.testing import CliRunner
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from celerp.db_url import sync_url
from test_db_compatibility import scratch  # noqa: F401 - fixture

EMAIL = "owner@example.com"
NEW_PW = "new-password1"
UNREACHABLE = "postgresql+asyncpg://celerp:celerp@127.0.0.1:1/nowhere"
NO_DATABASE = "Could not reach the database. Keep Celerp open and run the command again."


def _seed(url: str) -> uuid.UUID:
    """One owner with a session and an auth nonce; their id."""
    from celerp.models.auth import SessionRegistry, UserAuthState
    from celerp.models.company import Company, User
    from celerp.services.auth import hash_password

    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    user_id, company_id = uuid.uuid4(), uuid.uuid4()
    try:
        with Session(engine) as s, s.begin():
            s.add(Company(id=company_id, name="CLI Co", slug=f"cli-{company_id.hex[:8]}"))
            s.add(User(id=user_id, email=EMAIL, name="Owner Person", auth_hash=hash_password("old-password1"),
                       is_install_owner=True))
            s.flush()
            s.add(UserAuthState(user_id=user_id, nonce="nonce-before"))
            s.add(SessionRegistry(jti="jti-before", user_id=user_id, company_id=company_id,
                                  expiry=datetime.now(timezone.utc) + timedelta(hours=1)))
    finally:
        engine.dispose()
    return user_id


def _read(url: str, sql: str) -> list[tuple]:
    engine = sa.create_engine(sync_url(url), poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return [tuple(r) for r in conn.execute(sa.text(sql))]
    finally:
        engine.dispose()


def _run(monkeypatch, config_url: str, email: str = EMAIL, password: str = NEW_PW):
    from celerp import cli

    monkeypatch.setattr(cli, "_read_config", lambda: {"database": {"url": config_url}})
    return CliRunner().invoke(cli.main, ["reset-password"], input=f"{email}\n{password}\n{password}\n")


def test_cli_reset_uses_shared_reset(scratch, monkeypatch):
    """The command prompts for the email and the new password twice, then resets the
    way every reset does: new hash, every session ended, the reset recorded with the
    computer account that ran it, and the notice for the next sign-in."""
    from celerp.services.auth import verify_password

    url = scratch()
    user_id = _seed(url)
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "สมชาย")

    result = _run(monkeypatch, url)
    assert result.exit_code == 0, result.output
    assert "Password reset. Sign in with the new password." in result.output
    assert NEW_PW not in result.output

    [(auth_hash,)] = _read(url, f"SELECT auth_hash FROM users WHERE id = '{user_id}'")
    assert verify_password(NEW_PW, auth_hash)
    assert _read(url, "SELECT jti FROM session_registry") == []
    assert _read(url, "SELECT nonce FROM user_auth_state") != [("nonce-before",)]
    resets = _read(url, "SELECT id, user_id, method, os_user, by_user_id FROM password_resets")
    assert [r[1:] for r in resets] == [(user_id, "terminal", "สมชาย", None)]
    assert _read(url, "SELECT reset_id, recipient_id FROM password_reset_notices") == [(resets[0][0], user_id)]


def test_cli_reads_desktop_database_url_file(scratch, monkeypatch, tmp_path):
    """The desktop's database listens on a new port each start, so the command reads
    the address the app published in its data folder, not the one in the config."""
    url = scratch()
    _seed(url)
    (tmp_path / "database-url").write_text(url)
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))

    result = _run(monkeypatch, UNREACHABLE)
    assert result.exit_code == 0, result.output
    assert len(_read(url, "SELECT id FROM password_resets")) == 1


@pytest.mark.parametrize("where", ["config", "stale file"])
def test_cli_db_unreachable_message(monkeypatch, tmp_path, where):
    """A database that cannot be reached, including an address left by an earlier start,
    is one plain line and exit 1, never a stack of driver errors."""
    if where == "stale file":
        (tmp_path / "database-url").write_text(UNREACHABLE)
        monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    else:
        monkeypatch.delenv("CELERP_DATA_DIR", raising=False)

    result = _run(monkeypatch, UNREACHABLE)
    assert result.exit_code == 1, result.output
    assert NO_DATABASE in result.output
    assert "Error:" not in result.output and "Traceback" not in result.output


def test_cli_unknown_email_changes_nothing(scratch, monkeypatch):
    url = scratch()
    _seed(url)
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    before = _read(url, "SELECT auth_hash FROM users")

    result = _run(monkeypatch, url, email="nobody@example.com")
    assert result.exit_code == 1
    assert "No user found" in result.output
    assert _read(url, "SELECT auth_hash FROM users") == before
    assert _read(url, "SELECT id FROM password_resets") == []
