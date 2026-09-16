# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary

"""Team grace-period UI and packaged DB-state getter (PR6 app side).

Two groups:

* ``get_packaged_db_state()`` reads db_mode / external_db_url / feature_flags
  straight from celerp-config.json on disk (the cold-boot / relay-disconnected
  case the in-memory flags cannot serve), exposing only a ``has_external_url``
  boolean, never the URL string.
* ``_grace_notice()`` renders the grace-period banner and the after-grace
  persistent notice, with an actionable renewal control whose destination
  tracks the install's commercial mode, and resource-specific copy (external
  database vs external storage) rather than always DB-worded. The render tests
  register a sentinel language ``xx`` and assert the sentinel text reaches the
  output while ``xx`` is active.

``_has_team_features`` reads ``get_local_infra_state()`` rather than the
packaged-only ``get_packaged_db_state()``, so Team infrastructure stays
visible on a self-hosted build (and cross-build in general), not only inside
the Electron packaged app.

Red at merge-base: ``get_packaged_db_state`` and ``_grace_notice`` do not yet
exist, and ``_has_team_features`` ignores grace state.
"""

from datetime import datetime, timedelta, timezone

import json

import pytest
from fasthtml.common import to_xml

from ui import i18n
from ui.routes.settings_cloud import _grace_notice, _has_team_features
from celerp.gateway.state import (
    get_local_infra_state,
    get_packaged_db_state,
    set_feature_flags,
)


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


# ── get_local_infra_state excludes secrets (equivalent to the packaged-only
# assertion above, but for the getter _has_team_features/_grace_notice now
# actually consume) ─────────────────────────────────────────────────────────

def test_local_infra_state_excludes_url_value(tmp_path, monkeypatch):
    """get_local_infra_state carries a has_external_url boolean and never the
    URL string (which holds the DB password), on the packaged branch it
    projects from get_packaged_db_state."""
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    secret_url = "postgresql+asyncpg://celerp:s3cr3t@db.example.com:5432/celerp"
    _write_config(
        tmp_path,
        db_mode="external",
        external_db_url=secret_url,
        feature_flags={"external_db": True, "external_storage": False, "grace_period_ends": None},
    )
    state = get_local_infra_state()
    assert state["has_external_url"] is True
    assert "external_db_url" not in state
    assert "s3cr3t" not in json.dumps(state)


# ── _has_team_features grace-awareness ────────────────────────────────────────

def test_infra_visible_during_grace(tmp_path, monkeypatch):
    """During grace the fetched commercial state carries no team flags, but infra
    visibility must be retained: _has_team_features reads the on-disk grace state
    via get_local_infra_state (packaged branch, CELERP_DATA_DIR set)."""
    set_feature_flags({})
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    _write_config(
        tmp_path,
        db_mode="external",
        external_db_url="postgresql+asyncpg://celerp:x@db.example.com:5432/celerp",
        feature_flags={"external_db": False, "external_storage": False, "grace_period_ends": _future()},
    )
    assert _has_team_features({}) is True


def test_infra_visible_cross_build_self_hosted(monkeypatch):
    """Team infrastructure stays visible on a self-hosted build (no
    CELERP_DATA_DIR): _has_team_features reads get_local_infra_state, whose
    self-hosted branch derives has_external_url from the explicit external_db
    config opt-in rather than the Electron-only packaged config file."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    set_feature_flags({"external_db": False, "external_storage": False,
                        "grace_period_ends": _future()})
    from celerp.config import settings
    monkeypatch.setattr(settings, "external_db", True)
    assert _has_team_features({}) is True


# ── _grace_notice render (xx sentinel) ────────────────────────────────────────

_XX = {
    "grace.deadline": "XX_GRACE_DEADLINE {deadline}",
    "grace.external_owned": "XX_GRACE_OWNED",
    "grace.renew": "XX_GRACE_RENEW",
    "grace.local_now": "XX_GRACE_LOCALNOW",
    "grace.external_available": "XX_GRACE_EXTAVAIL",
    "grace.divergence_warning": "XX_GRACE_DIVERGENCE",
    "grace.storage_deadline": "XX_GRACE_STORAGE_DEADLINE {deadline}",
    "grace.storage_owned": "XX_GRACE_STORAGE_OWNED",
    "grace.storage_local_now": "XX_GRACE_STORAGE_LOCALNOW",
    "grace.storage_available": "XX_GRACE_STORAGE_AVAIL",
    "grace.storage_divergence_warning": "XX_GRACE_STORAGE_DIVERGENCE",
    "grace.both_deadline": "XX_GRACE_BOTH_DEADLINE {deadline}",
    "grace.both_owned": "XX_GRACE_BOTH_OWNED",
    "grace.both_local_now": "XX_GRACE_BOTH_LOCALNOW",
    "grace.both_available": "XX_GRACE_BOTH_AVAIL",
    "grace.both_divergence_warning": "XX_GRACE_BOTH_DIVERGENCE",
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


def _storage_grace_state() -> dict:
    """Storage-only lapse: no external DB configured, only external storage in
    grace. _grace_notice must use the storage-worded copy, never the DB copy."""
    return {
        "has_external_url": False,
        "external_db_entitled": True,
        "has_external_storage": True,
        "external_storage_entitled": False,
        "storage_in_grace": True,
        "in_grace": False,
        "grace_period_ends": _future(),
    }


def _storage_after_grace_state() -> dict:
    return {
        "has_external_url": False,
        "external_db_entitled": True,
        "has_external_storage": True,
        "external_storage_entitled": False,
        "storage_in_grace": False,
        "in_grace": False,
        "grace_period_ends": None,
    }


def _both_grace_state() -> dict:
    """Both DB and storage in grace at once: the combined copy names both
    resources, never just one."""
    return {
        "has_external_url": True,
        "external_db_entitled": False,
        "in_grace": True,
        "has_external_storage": True,
        "external_storage_entitled": False,
        "storage_in_grace": True,
        "grace_period_ends": _future(),
    }


def test_grace_notice_shows_deadline_and_renewal():
    """The grace banner shows the deadline and a renewal affordance."""
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    assert "XX_GRACE_DEADLINE" in html
    assert "XX_GRACE_RENEW" in html


def test_after_grace_notice_warns_divergence():
    """The after-grace notice warns that reselecting external risks divergence."""
    html = to_xml(_grace_notice(_after_grace_state(), lang="xx"))
    assert "XX_GRACE_DIVERGENCE" in html
    assert "XX_GRACE_LOCALNOW" in html


def test_grace_notice_neutral_without_partner():
    """With no partner identity, the notice renders neutral renewal copy and no
    partner support line, never a fabricated partner."""
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    assert "XX_GRACE_RENEW" in html
    assert "XX_GRACE_PARTNER" not in html
    assert "Acme Partners" not in html


# ── U4: resource-branched grace copy (DB vs storage vs both) ─────────────────

def test_grace_notice_db_only_uses_db_copy_not_storage():
    """A DB-only lapse uses the DB-worded strings, never the storage-worded
    ones (regression guard: pre-U4 the notice was always DB-worded)."""
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    assert "XX_GRACE_DEADLINE" in html
    assert "XX_GRACE_STORAGE_DEADLINE" not in html
    assert "XX_GRACE_BOTH_DEADLINE" not in html


def test_grace_notice_storage_only_uses_storage_copy():
    """A storage-only lapse uses the storage-worded strings, never the
    DB-worded default copy - this is the bug U4 fixes."""
    html = to_xml(_grace_notice(_storage_grace_state(), lang="xx"))
    assert "XX_GRACE_STORAGE_DEADLINE" in html
    assert "XX_GRACE_STORAGE_OWNED" in html
    assert "XX_GRACE_DEADLINE " not in html
    assert "XX_GRACE_OWNED" not in html


def test_after_grace_notice_storage_only_uses_storage_copy():
    html = to_xml(_grace_notice(_storage_after_grace_state(), lang="xx"))
    assert "XX_GRACE_STORAGE_LOCALNOW" in html
    assert "XX_GRACE_STORAGE_DIVERGENCE" in html
    assert "XX_GRACE_LOCALNOW" not in html
    assert "XX_GRACE_DIVERGENCE" not in html


def test_grace_notice_both_lapsed_uses_combined_copy():
    """Both DB and storage in grace at once: the combined copy names both
    resources rather than picking just one and silently dropping the other."""
    html = to_xml(_grace_notice(_both_grace_state(), lang="xx"))
    assert "XX_GRACE_BOTH_DEADLINE" in html
    assert "XX_GRACE_BOTH_OWNED" in html


# ── #4: grace classification gated on the configured resource ────────────────
#
# in_grace / storage_in_grace must be true only when the corresponding resource
# is actually configured. Pre-fix both getters computed each flag from the grace
# window and entitlement alone, so a DB-only install was also classed as storage
# grace (and vice versa) and a resource-less install was classed as both. These
# tests drive the ACTUAL getters (no manufactured state dict) for four fixtures
# each - DB-only, storage-only, both, neither - and feed the result into
# _grace_notice, asserting the resource-specific prefix and None for neither.

def _selfhosted(monkeypatch, *, external_db: bool, external_storage: bool) -> None:
    """Configure the self-hosted getter branch: no CELERP_DATA_DIR, the
    external-DB opt-in and the S3 backend set on the runtime Settings object."""
    monkeypatch.delenv("CELERP_DATA_DIR", raising=False)
    from celerp.config import settings
    monkeypatch.setattr(settings, "external_db", external_db, raising=False)
    monkeypatch.setattr(settings, "storage_backend", "s3" if external_storage else "local",
                        raising=False)
    monkeypatch.setattr(settings, "storage_s3_endpoint", "", raising=False)
    monkeypatch.setattr(settings, "storage_s3_bucket",
                        "bkt" if external_storage else "", raising=False)
    monkeypatch.setattr(settings, "storage_s3_access_key", "", raising=False)


def test_selfhosted_db_only_grace_is_db_not_storage(monkeypatch):
    _selfhosted(monkeypatch, external_db=True, external_storage=False)
    set_feature_flags({"external_db": False, "external_storage": False,
                       "grace_period_ends": _future()})
    state = get_local_infra_state()
    assert state["in_grace"] is True
    assert state["storage_in_grace"] is False
    html = to_xml(_grace_notice(state, lang="xx"))
    assert "XX_GRACE_DEADLINE" in html
    assert "XX_GRACE_STORAGE_DEADLINE" not in html
    assert "XX_GRACE_BOTH_DEADLINE" not in html


def test_selfhosted_storage_only_grace_is_storage_not_db(monkeypatch):
    _selfhosted(monkeypatch, external_db=False, external_storage=True)
    set_feature_flags({"external_db": False, "external_storage": False,
                       "grace_period_ends": _future()})
    state = get_local_infra_state()
    assert state["in_grace"] is False
    assert state["storage_in_grace"] is True
    html = to_xml(_grace_notice(state, lang="xx"))
    assert "XX_GRACE_STORAGE_DEADLINE" in html
    assert "XX_GRACE_BOTH_DEADLINE" not in html


def test_selfhosted_both_grace_uses_combined(monkeypatch):
    _selfhosted(monkeypatch, external_db=True, external_storage=True)
    set_feature_flags({"external_db": False, "external_storage": False,
                       "grace_period_ends": _future()})
    state = get_local_infra_state()
    assert state["in_grace"] is True
    assert state["storage_in_grace"] is True
    html = to_xml(_grace_notice(state, lang="xx"))
    assert "XX_GRACE_BOTH_DEADLINE" in html


def test_selfhosted_neither_resource_no_grace_no_notice(monkeypatch):
    _selfhosted(monkeypatch, external_db=False, external_storage=False)
    set_feature_flags({"external_db": False, "external_storage": False,
                       "grace_period_ends": _future()})
    state = get_local_infra_state()
    assert state["in_grace"] is False
    assert state["storage_in_grace"] is False
    assert _grace_notice(state, lang="xx") is None


def _packaged_config(tmp_path, monkeypatch, *, external_db: bool, external_storage: bool) -> None:
    monkeypatch.setenv("CELERP_DATA_DIR", str(tmp_path))
    top = {
        "db_mode": "external" if external_db else "local",
        "external_db_url": "postgresql+asyncpg://celerp:x@db.example.com:5432/celerp"
        if external_db else "",
        "storage_mode": "s3" if external_storage else "local",
        "feature_flags": {"external_db": False, "external_storage": False,
                          "grace_period_ends": _future()},
    }
    if external_storage:
        top["storage_s3_bucket"] = "bkt"
    _write_config(tmp_path, **top)


def test_packaged_db_only_grace_is_db_not_storage(tmp_path, monkeypatch):
    _packaged_config(tmp_path, monkeypatch, external_db=True, external_storage=False)
    state = get_packaged_db_state()
    assert state["in_grace"] is True
    assert state["storage_in_grace"] is False


def test_packaged_storage_only_grace_is_storage_not_db(tmp_path, monkeypatch):
    _packaged_config(tmp_path, monkeypatch, external_db=False, external_storage=True)
    state = get_packaged_db_state()
    assert state["in_grace"] is False
    assert state["storage_in_grace"] is True


def test_packaged_neither_resource_no_grace(tmp_path, monkeypatch):
    _packaged_config(tmp_path, monkeypatch, external_db=False, external_storage=False)
    state = get_packaged_db_state()
    assert state["in_grace"] is False
    assert state["storage_in_grace"] is False


# ── #3: the grace renewal affordance is an actionable control ────────────────
#
# The renewal line was dead prose (a partner-support sentence or nothing). It is
# now one actionable anchor whose (href, label) come from commercial_cta, so it
# tracks the install's commercial mode: the mint route on a direct install, the
# partner support URL/email (or Enterprise) on a partner-managed one. The neutral
# renewal hint stays.

import celerp.gateway.state as _gw_state


@pytest.fixture
def _reset_commercial_context():
    _gw_state._commercial_context = {}
    yield
    _gw_state._commercial_context = {}


class _Anchor:
    """The renewal anchor's href (XML-unescaped, so it compares against the raw
    canonical URL a resolver returns) and its visible label."""

    def __init__(self, href: str, label: str):
        self._href = href
        self._label = label

    def group(self, n: int) -> str:
        return self._href if n == 1 else self._label


def _renewal_anchor(html: str):
    import html as _html
    import re
    m = re.search(r'<a[^>]*href="([^"]+)"[^>]*>([^<]*)</a>', html)
    if not m:
        return None
    return _Anchor(_html.unescape(m.group(1)), _html.unescape(m.group(2)))


def test_renewal_cta_direct_points_at_mint_route(_reset_commercial_context):
    from ui.components.cloud_gate import subscribe_url
    from ui.i18n import t
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    assert "XX_GRACE_RENEW" in html  # neutral hint retained
    m = _renewal_anchor(html)
    assert m, "no actionable renewal anchor rendered"
    assert m.group(1) == subscribe_url("cloud")
    assert m.group(2) == t("cloud.start_trial", "xx")


def test_renewal_cta_partner_url_points_at_support(_reset_commercial_context):
    from ui.i18n import t
    _gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": {"display_name": "Acme Partners",
                           "support_url": "https://acme.example/support"},
    }
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    m = _renewal_anchor(html)
    assert m, "no actionable renewal anchor rendered"
    assert m.group(1) == "https://acme.example/support"
    assert m.group(2) == t("cloud.partner_support", "xx")
    assert "XX_GRACE_PARTNER" not in html  # old dead-prose key gone


def test_renewal_cta_partner_email_points_at_mailto(_reset_commercial_context):
    from ui.i18n import t
    _gw_state._commercial_context = {
        "commercial_mode": "partner_managed",
        "implementation": {"display_name": "Acme Partners",
                           "support_email": "help@acme.example"},
    }
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    m = _renewal_anchor(html)
    assert m and m.group(1) == "mailto:help@acme.example"
    assert m.group(2) == t("cloud.partner_support", "xx")


def test_renewal_cta_unknown_mode_fails_closed_to_enterprise(_reset_commercial_context):
    from celerp.gateway.state import enterprise_url
    from ui.i18n import t
    _gw_state._commercial_context = {"commercial_mode": "something_unexpected"}
    html = to_xml(_grace_notice(_grace_state(), lang="xx"))
    m = _renewal_anchor(html)
    assert m and m.group(1) == enterprise_url()
    assert m.group(2) == t("cloud.contact_celerp", "xx")
