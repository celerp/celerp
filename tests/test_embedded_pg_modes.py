# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Unit tests for embedded-vs-external database mode resolution (celerp.cli).

Layer 1 of the embedded-Postgres plan: pure, no database, no pgserver required.
Covers the decision table, probe classification, config round-trip, the Windows
getuid guard, and error messaging — including the invariants the DigitalOcean
droplet relies on (an explicit --db-url or an existing external config never
flips to embedded).
"""

from __future__ import annotations

import os

import pytest

from celerp.cli import (
    _classify_probe,
    _emit_db_error,
    _is_root,
    _resolve_db_mode,
    _read_config,
    _write_config,
)


# ── _resolve_db_mode: the full decision table ─────────────────────────────────
#
# Columns: db_url, server_reachable, provider_available, want_embedded,
#          no_embedded, existing_embedded  →  (mode, error_kind)

_CASES = [
    # id, kwargs, expected
    ("fresh_no_server_provider_yes", dict(db_url=None, server_reachable=False, provider_available=True,
                                          want_embedded=False, no_embedded=False, existing_embedded=None),
     ("embedded", None)),
    ("fresh_server_up_prefers_external", dict(db_url=None, server_reachable=True, provider_available=True,
                                              want_embedded=False, no_embedded=False, existing_embedded=None),
     ("external", None)),
    ("fresh_no_server_no_provider", dict(db_url=None, server_reachable=False, provider_available=False,
                                         want_embedded=False, no_embedded=False, existing_embedded=None),
     ("error", "no_server_no_provider")),
    ("explicit_db_url_is_external", dict(db_url="postgresql+asyncpg://u:p@h/db", server_reachable=None,
                                         provider_available=True, want_embedded=False, no_embedded=False,
                                         existing_embedded=None),
     ("external", None)),
    ("db_url_wins_even_with_provider", dict(db_url="postgresql+asyncpg://u:p@h/db", server_reachable=False,
                                            provider_available=True, want_embedded=False, no_embedded=False,
                                            existing_embedded=None),
     ("external", None)),
    ("force_embedded_over_running_server", dict(db_url=None, server_reachable=True, provider_available=True,
                                                want_embedded=True, no_embedded=False, existing_embedded=None),
     ("embedded", None)),
    ("force_embedded_without_provider_errors", dict(db_url=None, server_reachable=False, provider_available=False,
                                                    want_embedded=True, no_embedded=False, existing_embedded=None),
     ("error", "no_provider")),
    ("no_embedded_requires_server", dict(db_url=None, server_reachable=False, provider_available=True,
                                         want_embedded=False, no_embedded=True, existing_embedded=None),
     ("error", "no_server_external_only")),
    ("no_embedded_with_server_ok", dict(db_url=None, server_reachable=True, provider_available=True,
                                        want_embedded=False, no_embedded=True, existing_embedded=None),
     ("external", None)),
    ("conflicting_flags", dict(db_url=None, server_reachable=None, provider_available=True,
                               want_embedded=True, no_embedded=True, existing_embedded=None),
     ("error", "conflicting_flags")),
    ("db_url_plus_embedded_conflicts", dict(db_url="postgresql://x/y", server_reachable=None,
                                            provider_available=True, want_embedded=True, no_embedded=False,
                                            existing_embedded=None),
     ("error", "db_url_with_embedded")),
    # Existing-instance mode is preserved on --force (droplet contract).
    ("existing_external_stays_external", dict(db_url=None, server_reachable=False, provider_available=True,
                                              want_embedded=False, no_embedded=False, existing_embedded=False),
     ("external", None)),
    ("existing_embedded_stays_embedded", dict(db_url=None, server_reachable=True, provider_available=True,
                                              want_embedded=False, no_embedded=False, existing_embedded=True),
     ("embedded", None)),
    ("existing_embedded_but_no_provider", dict(db_url=None, server_reachable=False, provider_available=False,
                                               want_embedded=False, no_embedded=False, existing_embedded=True),
     ("error", "no_provider")),
]


@pytest.mark.parametrize("kwargs,expected", [(c[1], c[2]) for c in _CASES], ids=[c[0] for c in _CASES])
def test_resolve_db_mode_table(kwargs, expected):
    assert _resolve_db_mode(**kwargs) == expected


def test_existing_external_never_flips_to_embedded_when_server_down():
    """The core droplet invariant, called out explicitly: a re-init (--force) of
    an external install whose server is momentarily unreachable stays external —
    it must not start a second postmaster beside the real one."""
    mode, _ = _resolve_db_mode(
        db_url=None, server_reachable=False, provider_available=True,
        want_embedded=False, no_embedded=False, existing_embedded=False,
    )
    assert mode == "external"


# ── _classify_probe ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("err,expected", [
    (None, "ok"),
    ('database "celerp" does not exist', "needs_provision"),
    ('role "celerp" does not exist', "needs_provision"),
    ("password authentication failed for user", "needs_provision"),
    ("fe_sendauth: no password supplied", "needs_provision"),
    ("connection to server at localhost, port 5432 failed: Connection refused", "unreachable"),
    ("timeout expired", "unreachable"),
    ("could not translate host name", "unreachable"),
])
def test_classify_probe(err, expected):
    assert _classify_probe(err) == expected


def test_probe_needs_provision_counts_as_reachable():
    """A server that is up but missing the role/db must resolve to external mode
    (provision it), not embedded."""
    reachable = _classify_probe('database "celerp" does not exist') in ("ok", "needs_provision")
    mode, _ = _resolve_db_mode(
        db_url=None, server_reachable=reachable, provider_available=True,
        want_embedded=False, no_embedded=False, existing_embedded=None,
    )
    assert mode == "external"


# ── config round-trip: the `embedded` marker ──────────────────────────────────

@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    config_file = tmp_path / "celerp" / "config.toml"
    monkeypatch.setenv("CELERP_CONFIG", str(config_file))
    return config_file


def test_embedded_flag_persists(tmp_config):
    _write_config({"database": {"url": "postgresql+asyncpg://postgres@/celerp?host=/s", "embedded": True}})
    assert _read_config()["database"]["embedded"] is True


def test_missing_embedded_key_reads_as_external(tmp_config):
    """Every config written before this shipped (all droplets) has no `embedded`
    key — it must read as external, i.e. a falsy/absent flag."""
    _write_config({"database": {"url": "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp"}})
    cfg = _read_config()
    assert cfg["database"].get("embedded") in (None, False)


# ── Windows getuid guard ──────────────────────────────────────────────────────

def test_is_root_false_without_getuid(monkeypatch):
    """On Windows os.getuid does not exist; _is_root must return False, not raise."""
    monkeypatch.delattr(os, "getuid", raising=False)
    assert _is_root() is False


def test_is_root_true_as_uid_0(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0, raising=False)
    assert _is_root() is True


# ── error messaging ───────────────────────────────────────────────────────────

def test_no_provider_message_names_supported_platforms(capsys):
    _emit_db_error("no_provider", "postgresql://x/y")
    err = capsys.readouterr().err
    assert "not available on this platform" in err
    assert "x86_64/arm64" in err  # names the supported platform set


def test_no_server_no_provider_message_suggests_install(capsys):
    _emit_db_error("no_server_no_provider", "postgresql://localhost:5432/celerp")
    err = capsys.readouterr().err
    assert "No PostgreSQL server found" in err
    assert "Install PostgreSQL" in err


def test_no_server_external_only_message(capsys):
    _emit_db_error("no_server_external_only", "postgresql://localhost:5432/celerp")
    err = capsys.readouterr().err
    assert "--no-embedded" in err
