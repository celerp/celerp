# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Regression tests for per-xdist-worker config isolation (conftest_support).

The bug: xdist workers inherit the controller process's environment, where the
root conftest already set CELERP_CONFIG to the controller's ("main") temp path.
The old conftest used os.environ.setdefault, so that inherited value stuck and
EVERY worker read/wrote the SAME config.toml. Under -n auto two workers then
corrupted the file mid-write, and a torn read raised tomllib.TOMLDecodeError,
500ing unrelated requests (observed as flaky apply-preset failures on CI shard 6).

resolve_worker_config recomputes the path per worker whenever the inherited value
is one of our own temp paths, so two workers can never collide.
"""
from __future__ import annotations

from conftest_support import is_own_test_config, resolve_worker_config

_TMP = "/tmp"


def _own(worker: str) -> str:
    return f"{_TMP}/celerp-test-config-{worker}.toml"


class TestResolveWorkerConfig:
    def test_inherited_main_path_is_recomputed_for_the_worker(self):
        # The exact bug scenario: the worker inherits the controller's "main" path.
        # It must NOT keep it (that is what shared the file); it takes its own.
        assert resolve_worker_config(_own("main"), "gw1", _TMP) == _own("gw1")

    def test_two_workers_never_share_a_file(self):
        inherited = _own("main")
        p0 = resolve_worker_config(inherited, "gw0", _TMP)
        p1 = resolve_worker_config(inherited, "gw1", _TMP)
        assert p0 != p1
        assert p0 == _own("gw0")
        assert p1 == _own("gw1")

    def test_unset_env_gets_per_worker_path(self):
        assert resolve_worker_config(None, "gw3", _TMP) == _own("gw3")

    def test_external_config_is_left_untouched(self):
        external = "/etc/celerp/config.toml"
        assert resolve_worker_config(external, "gw0", _TMP) == external

    def test_main_controller_resolves_to_its_own_path(self):
        assert resolve_worker_config(None, "main", _TMP) == _own("main")


class TestIsOwnTestConfig:
    def test_none_is_not_ours(self):
        assert is_own_test_config(None, _TMP) is False

    def test_our_temp_path_is_ours(self):
        assert is_own_test_config(_own("gw0"), _TMP) is True

    def test_external_path_is_not_ours(self):
        assert is_own_test_config("/etc/celerp/config.toml", _TMP) is False

    def test_wrong_prefix_in_tmp_is_not_ours(self):
        assert is_own_test_config(f"{_TMP}/some-other-file.toml", _TMP) is False
