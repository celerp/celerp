# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Deployment-aware idle-logout policy.

Idle logout is an installation-level exposure policy: on whenever Celerp runs as
a server (headless/service-managed, or an active public URL), off for an ordinary
local desktop install. Cloud linkage or a gateway token alone is not exposure.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def clean_env(monkeypatch):
    """Neutralise the two environment signals so each case sets only what it tests."""
    monkeypatch.delenv("CELERP_MODE", raising=False)
    monkeypatch.delenv("CELERP_PUBLIC_URL", raising=False)
    from celerp.config import settings
    monkeypatch.setattr(settings, "idle_logout_minutes", 15, raising=False)
    monkeypatch.setattr(settings, "cloud_disconnected", False, raising=False)
    monkeypatch.setattr(settings, "celerp_public_url", "", raising=False)
    return monkeypatch


def _patch_config(monkeypatch, cfg):
    monkeypatch.setattr("celerp.config.read_config", lambda: cfg)


def test_desktop_local_no_public_url_is_off(clean_env):
    """Ordinary local desktop install: no headless, no public URL -> off."""
    from celerp.config import effective_idle_logout_minutes
    _patch_config(clean_env, {})
    assert effective_idle_logout_minutes() == 0


def test_cloud_token_without_public_url_is_off(clean_env):
    """A gateway token / cloud linkage without a public URL is not exposure -> off."""
    from celerp.config import effective_idle_logout_minutes
    _patch_config(clean_env, {"cloud": {"token": "gw-token-abc"}})
    assert effective_idle_logout_minutes() == 0


def test_connect_public_url_applies_timeout_even_for_local_request(clean_env):
    """A configured Connect public URL is exposure: the timeout applies even though
    this evaluation is installation-level, not request-origin."""
    from celerp.config import effective_idle_logout_minutes
    _patch_config(clean_env, {"cloud": {"public_url": "https://acme.celerp.com"}})
    assert effective_idle_logout_minutes() == 15


def test_explicit_disconnect_with_stale_public_url_is_off(clean_env):
    """Explicit Cloud disconnect suppresses the Connect branch even when a stale
    public_url lingers in config."""
    from celerp.config import effective_idle_logout_minutes
    _patch_config(clean_env, {"cloud": {"disconnected": True, "public_url": "https://old.celerp.com"}})
    assert effective_idle_logout_minutes() == 0


def test_explicit_public_url_env_with_cloud_disconnected_applies_timeout(clean_env):
    """An operator-managed CELERP_PUBLIC_URL is independent of Connect: it wins over
    a Cloud disconnect."""
    from celerp.config import effective_idle_logout_minutes
    clean_env.setenv("CELERP_PUBLIC_URL", "https://erp.self-hosted.example.com")
    _patch_config(clean_env, {"cloud": {"disconnected": True}})
    assert effective_idle_logout_minutes() == 15


def test_headless_env_without_connect_applies_timeout(clean_env):
    """Headless via CELERP_MODE is exposure regardless of Connect state."""
    from celerp.config import effective_idle_logout_minutes
    clean_env.setenv("CELERP_MODE", "headless")
    _patch_config(clean_env, {})
    assert effective_idle_logout_minutes() == 15


def test_headless_config_marker_applies_timeout(clean_env):
    """Headless via the persisted [server] headless marker is exposure."""
    from celerp.config import effective_idle_logout_minutes
    _patch_config(clean_env, {"server": {"headless": True}})
    assert effective_idle_logout_minutes() == 15


def test_headless_plus_cloud_disconnected_applies_timeout(clean_env):
    """A disconnect never overrides headless mode."""
    from celerp.config import effective_idle_logout_minutes
    _patch_config(clean_env, {"server": {"headless": True}, "cloud": {"disconnected": True}})
    assert effective_idle_logout_minutes() == 15


@pytest.mark.parametrize("cfg", [
    {},
    {"cloud": {"public_url": "https://acme.celerp.com"}},
    {"server": {"headless": True}},
    {"cloud": {"disconnected": True}},
])
def test_configured_zero_is_off_in_every_mode(clean_env, cfg):
    """idle_logout_minutes <= 0 is the explicit disable in every deployment mode."""
    from celerp.config import settings, effective_idle_logout_minutes
    clean_env.setattr(settings, "idle_logout_minutes", 0, raising=False)
    _patch_config(clean_env, cfg)
    assert effective_idle_logout_minutes() == 0


def test_config_read_failure_does_not_crash(clean_env):
    """A durable-config read failure degrades to the settings-only decision rather
    than crashing shell rendering."""
    from celerp.config import effective_idle_logout_minutes

    def _boom():
        raise OSError("config unreadable")

    clean_env.setattr("celerp.config.read_config", _boom)
    # No public URL / headless in settings -> off, and no exception raised.
    assert effective_idle_logout_minutes() == 0


def test_shell_idle_js_renders_effective_minutes_and_mechanics(clean_env):
    """The shell renderer injects the effective minutes on each render and keeps the
    existing idle-logout URL and activity mechanics."""
    from ui.components.shell import _idle_logout_js
    _patch_config(clean_env, {"cloud": {"public_url": "https://acme.celerp.com"}})
    js = _idle_logout_js()
    assert "15 * 60000" in js
    assert "/logout?reason=idle" in js
    assert "addEventListener" in js
    assert "next=" in js


def test_shell_idle_js_off_for_local_desktop(clean_env):
    """A local desktop install renders the no-op (0 minutes) idle timer."""
    from ui.components.shell import _idle_logout_js
    _patch_config(clean_env, {})
    js = _idle_logout_js()
    assert "0 * 60000" in js


def test_shell_idle_js_does_not_crash_on_config_failure(clean_env):
    """Shell rendering survives a config read failure."""
    from ui.components.shell import _idle_logout_js

    def _boom():
        raise OSError("config unreadable")

    clean_env.setattr("celerp.config.read_config", _boom)
    js = _idle_logout_js()
    assert "0 * 60000" in js
