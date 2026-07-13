# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""First-admin setup-code guard for headless (network-exposed) installs."""

from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from celerp.cli import _config_path, _read_config, _write_config, main
from celerp.config import read_config, write_config


_REG = {"company_name": "C", "email": "a@b.com", "name": "A", "password": "password123"}


@pytest.fixture()
def code_config(tmp_path, monkeypatch):
    """Point config at a temp file that requires a known setup code."""
    cf = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cf))
    code = "abcd1234"
    write_config({
        "database": {"url": "postgresql+asyncpg://x/y"},
        "auth": {"jwt_secret": "s" * 64, "setup_code_hash": hashlib.sha256(code.encode()).hexdigest()},
        "server": {"api_port": 8000, "ui_port": 8080, "headless": True},
        "cloud": {"token": ""},
    })
    return code


# ── API guard ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_rejected_without_code(client, code_config):
    r = await client.post("/auth/register", json=_REG)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_register_rejected_with_wrong_code(client, code_config):
    r = await client.post("/auth/register", json={**_REG, "setup_code": "wrong"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_register_succeeds_with_code_and_consumes_it(client, code_config):
    r = await client.post("/auth/register", json={**_REG, "setup_code": code_config})
    assert r.status_code == 200, r.text
    # One-time: the hash is cleared, and the headless marker is preserved.
    cfg = read_config()
    assert not cfg.get("auth", {}).get("setup_code_hash")
    assert cfg.get("server", {}).get("headless") is True


@pytest.mark.asyncio
async def test_bootstrap_status_flags_setup_code_required(client, code_config):
    r = await client.get("/auth/bootstrap-status")
    assert r.json()["setup_code_required"] is True


@pytest.mark.asyncio
async def test_no_code_required_by_default(client, tmp_path, monkeypatch):
    cf = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(cf))
    write_config({"database": {"url": "postgresql+asyncpg://x/y"},
                  "auth": {"jwt_secret": "s" * 64},
                  "server": {"api_port": 8000, "ui_port": 8080}, "cloud": {"token": ""}})
    r = await client.post("/auth/register", json=_REG)
    assert r.status_code == 200, r.text
    assert (await client.get("/auth/bootstrap-status")).json()["setup_code_required"] is False


# ── CLI: `init --no-start` mints the code + headless marker ──────────────────

@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CELERP_CONFIG", str(tmp_path / "celerp" / "config.toml"))


def test_init_no_start_mints_setup_code(tmp_config):
    runner = CliRunner()
    with patch("celerp.cli._test_db", return_value=None), \
         patch("celerp.cli._run_migrations"), \
         patch("celerp.cli._post_migration_grants"), \
         patch("celerp.cli._needs_ownership_fix", return_value=False), \
         patch("celerp.cli._start") as mock_start:
        result = runner.invoke(main, ["init", "--no-start",
                                      "--db-url", "postgresql+asyncpg://celerp:celerp@localhost/celerp"])
    assert result.exit_code == 0, result.output
    mock_start.assert_not_called()

    cfg = _read_config()
    stored = cfg["auth"]["setup_code_hash"]
    assert stored
    assert cfg["server"]["headless"] is True

    code_file = _config_path().parent / "setup-code"
    assert code_file.exists()
    code = code_file.read_text().strip()
    assert hashlib.sha256(code.encode()).hexdigest() == stored
    assert "Setup code:" in result.output


def test_setup_form_shows_code_field_only_when_required():
    from fasthtml.common import to_xml
    from ui.routes.auth import _setup_form
    assert 'name="setup_code"' in to_xml(_setup_form(setup_code_required=True))
    assert 'name="setup_code"' not in to_xml(_setup_form(setup_code_required=False))


def test_init_without_no_start_has_no_setup_code(tmp_config):
    runner = CliRunner()
    with patch("celerp.cli._test_db", return_value=None), \
         patch("celerp.cli._run_migrations"), \
         patch("celerp.cli._post_migration_grants"), \
         patch("celerp.cli._needs_ownership_fix", return_value=False), \
         patch("celerp.cli._start"):
        result = runner.invoke(main, ["init",
                                      "--db-url", "postgresql+asyncpg://celerp:celerp@localhost/celerp"])
    assert result.exit_code == 0, result.output
    cfg = _read_config()
    assert not cfg["auth"].get("setup_code_hash")
    assert not cfg["server"].get("headless")
