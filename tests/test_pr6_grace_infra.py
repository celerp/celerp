# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Team grace-period UI and packaged DB-state getter (PR6 app side).

Two groups:

* ``get_packaged_db_state()`` reads db_mode / external_db_url / feature_flags
  straight from celerp-config.json on disk (the cold-boot / relay-disconnected
  case the in-memory flags cannot serve), exposing only a ``has_external_url``
  boolean, never the URL string.
* ``_grace_notice()`` renders the grace-period banner and the after-grace
  persistent notice, partner-aware when a partner identity is present and
  neutral otherwise. The render tests register a sentinel language ``xx`` and
  assert the sentinel text reaches the output while ``xx`` is active.

Red at merge-base: ``get_packaged_db_state`` and ``_grace_notice`` do not yet
exist, and ``_has_team_features`` ignores grace state.
"""

from datetime import datetime, timedelta, timezone

import json

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_cloud import _grace_notice, _has_team_features
from celerp.gateway.state import get_packaged_db_state, set_feature_flags


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()


def _past() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _write_config(data_dir, **top) -> None:
    (data_dir / "celerp-config.json").write_text(json.dumps(top))


# ── get_packaged_db_state ─────────────────────────────────────────────────────

def test_packaged_db_state_excludes_url_value(tmp_path, monkeypatch):
    """The state carries a has_external_url boolean and never the URL string
    (which holds the DB password)."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    secret_url = "postgresql+asyncpg://celerp:s3cr3t@db.example.com:5432/celerp"
    _write_config(
        tmp_path,
        db_mode="external",
        external_db_url=secret_url,
        feature_flags={"external_db": True, "external_storage": False, "grace_period_ends": None},
    )
    state = get_packaged_db_state()
    assert state["has_external_url"] is True
    assert "external_db_url" not in state
    assert "s3cr3t" not in json.dumps(state)


def test_packaged_db_state_tolerates_missing_config(tmp_path, monkeypatch):
    """A missing config file degrades to a neutral state, never an exception."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))  # dir exists, file does not
    state = get_packaged_db_state()
    assert state["external_db_entitled"] is False
    assert state["in_grace"] is False
    assert state["has_external_url"] is False


# ── _has_team_features grace-awareness ────────────────────────────────────────

def test_infra_visible_during_grace(tmp_path, monkeypatch):
    """During grace the fetched commercial state carries no team flags, but infra
    visibility must be retained: _has_team_features reads the on-disk grace state."""
    set_feature_flags({})
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _write_config(
        tmp_path,
        db_mode="external",
        external_db_url="postgresql+asyncpg://celerp:x@db.example.com:5432/celerp",
        feature_flags={"external_db": False, "external_storage": False, "grace_period_ends": _future()},
    )
    assert _has_team_features({}) is True


# ── _grace_notice render (xx sentinel) ────────────────────────────────────────

_XX = {
    "grace.deadline": "XX_GRACE_DEADLINE {deadline}",
    "grace.external_owned": "XX_GRACE_OWNED",
    "grace.renew": "XX_GRACE_RENEW",
    "grace.partner_support": "XX_GRACE_PARTNER {partner}",
    "grace.local_now": "XX_GRACE_LOCALNOW",
    "grace.external_available": "XX_GRACE_EXTAVAIL",
    "grace.divergence_warning": "XX_GRACE_DIVERGENCE",
}

_PARTNER = {
    "display_name": "Acme Partners",
    "support_email": "help@acme.example",
    "support_url": "https://acme.example/support",
}


@pytest.fixture(autouse=True)
def _xx_lang():
    i18n.clear_registry()
    i18n._cached_load.cache_clear()
    i18n.register_catalog("xx", _XX)
    i18n.set_lang("xx")
    yield
    i18n.set_lang("en")
    i18n.clear_registry()
    i18n._cached_load.cache_clear()


def _grace_state() -> dict:
    return {
        "db_mode": "external",
        "has_external_url": True,
        "external_db_entitled": False,
        "in_grace": True,
        "grace_period_ends": _future(),
    }


def _after_grace_state() -> dict:
    return {
        "db_mode": "local",
        "has_external_url": True,
        "external_db_entitled": False,
        "in_grace": False,
        "grace_period_ends": None,
    }


def test_grace_notice_shows_deadline_and_renewal():
    """The grace banner shows the deadline and a renewal affordance."""
    html = to_xml(_grace_notice(_grace_state(), _PARTNER, lang="xx"))
    assert "XX_GRACE_DEADLINE" in html
    assert "XX_GRACE_RENEW" in html


def test_after_grace_notice_warns_divergence():
    """The after-grace notice warns that reselecting external risks divergence."""
    html = to_xml(_grace_notice(_after_grace_state(), None, lang="xx"))
    assert "XX_GRACE_DIVERGENCE" in html
    assert "XX_GRACE_LOCALNOW" in html


def test_grace_notice_neutral_without_partner():
    """With no partner identity, the notice renders neutral renewal copy and no
    partner support line, never a fabricated partner."""
    html = to_xml(_grace_notice(_grace_state(), None, lang="xx"))
    assert "XX_GRACE_RENEW" in html
    assert "XX_GRACE_PARTNER" not in html
    assert "Acme Partners" not in html
