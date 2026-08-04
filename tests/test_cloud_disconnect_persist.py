# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""A sticky Cloud disconnect survives a restart, even with an env credential.

Disconnect preserves the credential in [cloud] for one-click reconnect but marks
the install disconnected, and load_cloud_config must not bring that credential live
on the next boot. gateway_token and celerp_public_url are also plain env-bound
settings (GATEWAY_TOKEN / CELERP_PUBLIC_URL), which pydantic reads at construction,
before load_cloud_config runs. Without an override the env value slips past the
disconnect suppression, so the app reports the instance as token-bound and the
settings UI shows it reconnected. load_cloud_config clears the live credential when
disconnected so the whole app treats the install as disconnected until an explicit
reconnect applies a fresh one.
"""

from __future__ import annotations

import os

os.environ.setdefault("ALLOW_INSECURE_JWT", "true")

import celerp.config as cfg
from celerp.config import settings


def test_env_credential_suppressed_when_config_disconnected(monkeypatch):
    """An env-supplied gateway_token/public_url is dropped on boot when [cloud] is
    disconnected: the sticky disconnect wins over the environment."""
    monkeypatch.setattr(
        cfg, "read_config",
        lambda: {"cloud": {"disconnected": True, "token": "preserved-in-config",
                           "instance_id": "iid-x", "public_url": "https://config.example"}},
    )
    # As GATEWAY_TOKEN / CELERP_PUBLIC_URL would bind them at Settings construction.
    monkeypatch.setattr(settings, "gateway_token", "env-token")
    monkeypatch.setattr(settings, "celerp_public_url", "https://env.example")
    monkeypatch.setattr(settings, "cloud_disconnected", False)
    monkeypatch.setattr(settings, "gateway_instance_id", "")

    cfg.load_cloud_config()

    assert settings.cloud_disconnected is True
    assert settings.gateway_token == ""
    assert settings.celerp_public_url == ""
    # Identity still loads: it is the install, not the live connection.
    assert settings.gateway_instance_id == "iid-x"


def test_env_credential_preserved_when_not_disconnected(monkeypatch):
    """The disconnect suppression is scoped to a disconnected install: a normal
    env-configured instance keeps its credential."""
    monkeypatch.setattr(cfg, "read_config", lambda: {"cloud": {"instance_id": "iid-y"}})
    monkeypatch.setattr(settings, "gateway_token", "env-token")
    monkeypatch.setattr(settings, "celerp_public_url", "https://env.example")
    monkeypatch.setattr(settings, "cloud_disconnected", False)

    cfg.load_cloud_config()

    assert settings.cloud_disconnected is False
    assert settings.gateway_token == "env-token"
    assert settings.celerp_public_url == "https://env.example"
