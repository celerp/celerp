# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: BUSL-1.1
"""Sign-in, account and maintenance messages say what happened and what to do next."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from ui.i18n import t

_LOCALES = Path(__file__).resolve().parents[1] / "ui" / "locales"


@pytest.mark.asyncio
async def test_wrong_password_says_check_both_or_reset(client):
    r = await client.post("/auth/login", json={"email": "nobody@test.com", "password": "anything"})
    assert r.status_code == 401
    assert r.json()["detail"] == t("auth.invalid_credentials", "en")
    assert "Forgot password?" in r.json()["detail"]


def test_invalid_credentials_names_forgot_password_in_every_locale():
    for path in _LOCALES.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["auth.forgot_password"] in data["auth.invalid_credentials"], path.name


@pytest.mark.asyncio
async def test_bad_reset_link_says_how_to_get_a_new_one(client):
    r = await client.post("/auth/password-reset/confirm",
                          json={"token": "totally-wrong-token", "new_password": "newpassword1"})
    assert r.status_code == 400
    assert r.json()["detail"] == t("auth.reset_link_invalid", "en")
    assert "Forgot password?" in r.json()["detail"]


def test_wrong_setup_code_says_where_the_code_is(monkeypatch):
    from celerp.services import bootstrap
    monkeypatch.setattr(bootstrap, "setup_code_hash", lambda: "0" * 64)
    with pytest.raises(HTTPException) as exc:
        bootstrap.verify_setup_code("nope")
    assert exc.value.detail == t("auth.setup_code_invalid", "en")
    assert "setup-code" in exc.value.detail


def test_shared_constants_are_gone():
    from celerp.services import auth
    assert not hasattr(auth, "STAGED_COMPANY")
    assert auth.HAS_COMPANY == "auth.has_company"


def test_dead_password_keys_removed():
    en = json.loads((_LOCALES / "en.json").read_text(encoding="utf-8"))
    for key in ("error.password_too_short", "error.passwords_no_match", "error.email_password_required"):
        assert key not in en


@pytest.mark.parametrize("key, says", [
    ("auth.has_company", "Sign in with it"),
    ("auth.company_staged", "continue or discard"),
    ("auth.company_access_denied", "Ask the company owner"),
    ("auth.password_too_short", "at least 8 characters"),
    ("auth.current_password_wrong", "Caps Lock"),
    ("auth.install_owner_required", "Only the installation owner"),
    ("auth.server_error", "GitHub"),
    ("auth.setup_code_required", "setup-code"),
    ("auth.import_failed", "GitHub"),
    ("auth.import_failed_unreadable", "Refresh the page"),
    ("auth.import_timed_out", "Refresh the page"),
    ("auth.connection_error", "still running"),
    ("auth.reset_failed", "GitHub"),
    ("auth.email_password_required", "Enter your email and password."),
    ("error.session_expired", "You were signed out."),
    ("error.name_email_password_required", "Enter a name, an email and a password."),
    ("account.status_unreachable", "internet connection"),
    ("error.upload_too_large", "smaller file"),
    ("error.maintenance", "Wait a minute"),
    ("error.recovery_incomplete", "Restart Celerp"),
    ("api.export_timed_out", "filter"),
    ("api.backup_timed_out", "Nothing was changed"),
])
def test_auth_messages_say_what_to_do(key, says):
    assert says in t(key, "en", e="x", exc="x")


@pytest.mark.parametrize("key", ["api.export_timed_out", "api.backup_timed_out"])
def test_timeouts_translated(key):
    assert t(key, "de") != t(key, "en")


@pytest.mark.asyncio
async def test_export_timeout_uses_the_key(monkeypatch):
    import httpx
    from ui import api_client

    class _Client:
        def build_request(self, *a, **k):
            return None

        async def send(self, *a, **k):
            raise httpx.ReadTimeout("slow")

        async def aclose(self):
            return None

    monkeypatch.setattr(api_client, "_local_client", lambda *a, **k: _Client())
    with pytest.raises(api_client.APIError) as exc:
        await api_client.export_docs_csv("tok")
    assert exc.value.detail == t("api.export_timed_out", "en")
    with pytest.raises(api_client.APIError) as exc:
        await api_client.export_backup("tok")
    assert exc.value.detail == t("api.backup_timed_out", "en")


@pytest.mark.asyncio
async def test_oversized_upload_message(client):
    r = await client.post("/auth/login", content=b"x",
                          headers={"content-length": str(10 ** 12), "content-type": "application/json"})
    assert r.status_code == 413
    assert r.json()["detail"] == t("error.upload_too_large", "en")
