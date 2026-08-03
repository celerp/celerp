# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for /companies/me/modules API endpoints.

GET  /companies/me/modules
POST /companies/me/modules/{name}/enable
POST /companies/me/modules/{name}/disable

Uses the standard test client + register pattern from conftest.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.xdist_group("modules_api")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _register(client, role: str = "admin") -> str:
    email = f"mod-test-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Mod Test Co", "email": email, "name": "Admin", "password": "pw123"},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestModulesAPIEndpoints:
    @pytest.mark.asyncio
    async def test_list_modules_unauthenticated(self, client):
        r = await client.get("/companies/me/modules")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_list_modules_authenticated_returns_list(self, client):
        token = await _register(client)
        r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_list_modules_empty_module_dir(self, client, monkeypatch):
        """With no module directory, list returns empty list."""
        monkeypatch.delenv("MODULE_DIR", raising=False)
        token = await _register(client)
        r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200
        assert r.json() == []

    @pytest.mark.asyncio
    async def test_enable_module_unauthenticated(self, client):
        r = await client.post("/companies/me/modules/gemstones/enable")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_module_unauthenticated(self, client):
        r = await client.post("/companies/me/modules/celerp-labels/disable")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_enable_module_persists_to_settings(self, client):
        """Enabling a module adds it to company.settings enabled_modules."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert data.get("restart_required") is True
        assert "gemstones" in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_disable_module_removes_from_settings(self, client):
        """Disabling a module removes it from company.settings enabled_modules."""
        token = await _register(client)
        # First enable it
        await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        # Then disable
        r = await client.post("/companies/me/modules/gemstones/disable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert "gemstones" not in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_enable_then_disable_is_idempotent(self, client):
        """Double enable is safe; enabled set is a set (no duplicates)."""
        token = await _register(client)
        await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        r = await client.post("/companies/me/modules/gemstones/enable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        enabled = data.get("enabled_modules", [])
        assert enabled.count("gemstones") == 1  # No duplicate

    @pytest.mark.asyncio
    async def test_disable_not_enabled_module_is_safe(self, client):
        """Disabling a module that isn't enabled returns 200, no error."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/nonexistent-module/disable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert "nonexistent-module" not in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_enable_returns_restart_required(self, client):
        """Enable response always includes restart_required: true."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        assert r.status_code == 200
        assert r.json().get("restart_required") is True

    @pytest.mark.asyncio
    async def test_disable_returns_restart_required(self, client):
        """Disable response always includes restart_required: true."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/celerp-labels/disable", headers=_h(token))
        assert r.status_code == 200
        assert r.json().get("restart_required") is True

    @pytest.mark.asyncio
    async def test_company_isolation_module_settings(self, client):
        """Module settings are per-company, not global.
        
        Company A enables gemstones. Company B (created via POST /companies)
        should start with default enabled set, not A's settings.
        """
        # Register company A (bootstrap)
        token_a = await _register(client)
        # Create company B using the bootstrap token
        r_b = await client.post(
            "/companies",
            json={"name": "Company B"},
            headers=_h(token_a),
        )
        assert r_b.status_code == 200, r_b.text
        token_b = r_b.json()["access_token"]

        # Company A enables gemstones
        await client.post("/companies/me/modules/gemstones/enable", headers=_h(token_a))

        # Company B should NOT see gemstones in its settings (it has its own settings)
        r_b_list = await client.get("/companies/me/modules", headers=_h(token_b))
        assert r_b_list.status_code == 200
        # Verify companies have separate settings by checking enabled state
        r_b_disable = await client.post(
            "/companies/me/modules/gemstones/disable", headers=_h(token_b)
        )
        assert r_b_disable.status_code == 200
        data_b = r_b_disable.json()
        # B's disable call should NOT return gemstones in the enabled list
        assert "gemstones" not in data_b.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_list_modules_with_installed_module(self, client, tmp_path):
        """With a module installed, list includes it with enabled/running state."""
        import shutil
        from pathlib import Path
        import os

        token = await _register(client)

        # Copy celerp-verticals into a tmp module dir and point the loader at it
        verticals_src = Path(__file__).parent.parent.parent / "default_modules" / "celerp-verticals"

        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        shutil.copytree(verticals_src, module_dir / "celerp-verticals")

        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.get("/companies/me/modules", headers=_h(token))

        assert r.status_code == 200
        data = r.json()
        names = [m["name"] for m in data]
        assert "celerp-verticals" in names

        gem = next(m for m in data if m["name"] == "celerp-verticals")
        assert "enabled" in gem
        assert "running" in gem
        assert "version" in gem

    @pytest.mark.asyncio
    async def test_list_modules_disabled_module_has_metadata(self, client, tmp_path):
        """A disabled (not running) module must still return description, author, version."""
        import shutil
        import os

        token = await _register(client)

        # Use celerp-manufacturing which is MIT and has rich manifest fields
        mfg_src = Path(__file__).parent.parent.parent / "default_modules" / "celerp-manufacturing"
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        shutil.copytree(mfg_src, module_dir / "celerp-manufacturing")

        # Do NOT enable the module — it must appear as disabled
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.get("/companies/me/modules", headers=_h(token))

        assert r.status_code == 200
        data = r.json()
        names = [m["name"] for m in data]
        assert "celerp-manufacturing" in names

        m = next(x for x in data if x["name"] == "celerp-manufacturing")
        assert m["enabled"] is False
        assert m["running"] is False
        # These must be populated even when not running
        assert m["description"] == "Manufacturing orders, BOM management, and production tracking."
        assert m["author"] == "Celerp"
        assert m["version"] == "0.1.0"
        assert m["label"] == "Manufacturing"

    @pytest.mark.asyncio
    async def test_core_folded_modules_report_running(self, client):
        """REGRESSION: core-folded modules (ai/backup/connectors) are wired directly
        into the app at construction, NOT via the pluggable loader, so they never
        appear in loaded_modules(). list_modules must still report them running=True —
        otherwise the setup /activating page waits forever and shows "Some modules
        failed to start" (the AI/Cloud-Backup-spin-forever bug)."""
        from celerp.modules.loader import CORE_FOLDED

        token = await _register(client)
        default_modules = Path(__file__).parent.parent.parent / "default_modules"
        with patch.dict(os.environ, {"MODULE_DIR": str(default_modules)}):
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200
        by_name = {m["name"]: m for m in r.json()}

        # Every folded module present on disk must be reported running.
        on_disk_folded = [n for n in CORE_FOLDED if (default_modules / n).is_dir()]
        assert "celerp-ai" in on_disk_folded and "celerp-backup" in on_disk_folded, \
            "AI + Backup must be on disk for this regression test to be meaningful"
        for name in on_disk_folded:
            assert name in by_name, f"folded module {name} missing from /me/modules"
            assert by_name[name]["running"] is True, (
                f"folded module {name} reported running=False — the activating page "
                "would spin forever waiting for it"
            )

    @pytest.mark.asyncio
    async def test_is_running_counts_folded_modules(self):
        """Unit guard for the single source of truth used by the activating page."""
        from celerp.modules.loader import is_core_folded, is_running

        assert is_core_folded("celerp-ai") and is_core_folded("celerp-backup")
        assert not is_core_folded("celerp-verticals")
        # Folded modules are running even with an empty pluggable-loader registry.
        assert is_running("celerp-ai") is True
        assert is_running("celerp-verticals") is False

    @pytest.mark.asyncio
    async def test_subscriptions_outer_manifest_is_literal(self):
        """Outer celerp-subscriptions/__init__.py must define PLUGIN_MANIFEST as a dict literal.

        read_manifest_metadata uses AST parsing and cannot follow import statements.
        If the outer __init__.py uses 'from celerp_subscriptions import PLUGIN_MANIFEST',
        disabled-state rendering falls back to the raw package name instead of display_name.
        """
        from pathlib import Path
        from celerp.modules.loader import read_manifest_metadata

        subs_path = Path(__file__).parent.parent.parent / "default_modules" / "celerp-subscriptions"
        meta = read_manifest_metadata(subs_path)
        assert meta.get("display_name") == "Subscriptions", (
            "Outer __init__.py must define PLUGIN_MANIFEST as a literal dict with "
            f"display_name='Subscriptions'; got: {meta}"
        )
        assert meta.get("description"), "description must be non-empty"
        assert meta.get("version"), "version must be non-empty"


# ── provenance scan + delete lifecycle ────────────────────────────────────────

_PKG_INIT = ('PLUGIN_MANIFEST = {{"name": "{name}", "version": "1.0.0", '
             '"display_name": "{disp}"}}\n')


def _write_pkg(dirpath: Path, name: str) -> Path:
    pkg = dirpath / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_PKG_INIT.format(name=name, disp=name.title()))
    return pkg


class TestModuleProvenanceAndDelete:
    @pytest.mark.asyncio
    async def test_scan_reports_source_and_installed_at_for_import(self, client, tmp_path):
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        pkg = _write_pkg(module_dir, "acme-widgets")
        (pkg / ".celerp-meta.json").write_text(
            '{"source": "community", "installed_at": "2026-07-29T00:00:00+00:00"}')
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        row = next(m for m in r.json() if m["name"] == "acme-widgets")
        assert row["source"] == "community"
        assert row["installed_at"] == "2026-07-29T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_scan_reports_default_source_for_genuine_defaults(self, client):
        # Genuine, unmodified defaults (content matches the committed lock) scan
        # as source="default"; the content-identity predicate is the oracle.
        from celerp.modules.loader import is_first_party
        token = await _register(client)
        default_modules = Path(__file__).parent.parent.parent / "default_modules"
        with patch.dict(os.environ, {"MODULE_DIR": str(default_modules)}):
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200
        seen_default = False
        for m in r.json():
            if is_first_party(default_modules / m["name"]):
                seen_default = True
                assert m["source"] == "default", m
                assert m["installed_at"] is None, m
                assert m["demoted"] is False, m
        assert seen_default, "expected at least one default module in the scan"

    @pytest.mark.asyncio
    async def test_stray_folder_in_bundled_dir_not_in_lock_scans_non_default(
            self, client, tmp_path, monkeypatch):
        # Journey 2: a folder physically inside the bundled dir whose name is not a
        # shipped default (not in the lock) scans as NON-default, even though the
        # old name-listing check would have called it default.
        from celerp.modules import loader
        token = await _register(client)
        bundled = tmp_path / "default_modules"
        bundled.mkdir()
        _write_pkg(bundled, "celerp-strayxyz")
        monkeypatch.setattr(loader, "_BUNDLED_MODULES_DIRS", (bundled,))
        with patch.dict(os.environ, {"MODULE_DIR": str(bundled)}):
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        row = next(m for m in r.json() if m["name"] == "celerp-strayxyz")
        assert row["is_default"] is False
        assert row["source"] != "default"
        # Not named in the lock, so it is a stray - never a demoted default.
        assert row["demoted"] is False

    @pytest.mark.asyncio
    async def test_scan_reports_real_source_for_demoted_module(self, client, tmp_path):
        # A folder named after a real default but with junk content (the impostor)
        # scans as non-default, not "default".
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg(module_dir, "celerp-manufacturing")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        row = next(m for m in r.json() if m["name"] == "celerp-manufacturing")
        assert row["is_default"] is False
        assert row["source"] != "default"
        # Named in the lock but content mismatch: the scan reports the demotion
        # itself, so the UI banner needs no cross-render state.
        assert row["demoted"] is True

    @pytest.mark.asyncio
    async def test_delete_allows_demoted_module_previously_refused_as_default(
            self, client, tmp_path):
        # The impostor (real default name, junk content) is deletable now, where
        # the old name-based guard refused it as a default.
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg(module_dir, "celerp-manufacturing")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/celerp-manufacturing/delete", headers=_h(token))
        assert r.status_code == 200, r.text
        assert not (module_dir / "celerp-manufacturing").exists()

    @pytest.mark.asyncio
    async def test_delete_module_unauthenticated(self, client):
        r = await client.post("/companies/me/modules/x/delete")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_delete_disabled_nondefault_module_removes_folder_and_frees_name(
            self, client, tmp_path):
        from celerp.modules.importer import install_from_folder
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        src = _write_pkg(tmp_path / "src", "acme-widgets")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            install_from_folder(src)
            assert (module_dir / "acme-widgets").exists()
            r = await client.post(
                "/companies/me/modules/acme-widgets/delete", headers=_h(token))
            assert r.status_code == 200, r.text
            assert not (module_dir / "acme-widgets").exists()
            # The name is freed: the same folder re-imports cleanly.
            info = install_from_folder(src)
            assert info["name"] == "acme-widgets"
            assert (module_dir / "acme-widgets").exists()

    @pytest.mark.asyncio
    async def test_delete_default_module_refused(self, client):
        token = await _register(client)
        default_modules = Path(__file__).parent.parent.parent / "default_modules"
        with patch.dict(os.environ, {"MODULE_DIR": str(default_modules)}):
            r = await client.post(
                "/companies/me/modules/celerp-labels/delete", headers=_h(token))
        assert r.status_code in (409, 422), r.text
        assert (default_modules / "celerp-labels").exists()

    @pytest.mark.asyncio
    async def test_delete_enabled_module_refused(self, client, tmp_path):
        from celerp.modules.importer import install_from_folder
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        src = _write_pkg(tmp_path / "src", "acme-widgets")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            install_from_folder(src)
            re = await client.post(
                "/companies/me/modules/acme-widgets/enable", headers=_h(token))
            assert re.status_code == 200, re.text
            r = await client.post(
                "/companies/me/modules/acme-widgets/delete", headers=_h(token))
        assert r.status_code in (409, 422), r.text
        assert (module_dir / "acme-widgets").exists()

    @pytest.mark.asyncio
    async def test_delete_running_module_refused(self, client):
        # celerp-ai is core-folded (is_running True) and default; delete refused.
        token = await _register(client)
        default_modules = Path(__file__).parent.parent.parent / "default_modules"
        with patch.dict(os.environ, {"MODULE_DIR": str(default_modules)}):
            r = await client.post(
                "/companies/me/modules/celerp-ai/delete", headers=_h(token))
        assert r.status_code in (409, 422), r.text

    @pytest.mark.asyncio
    async def test_delete_nonexistent_module_404s(self, client, tmp_path):
        from celerp.modules.importer import install_from_folder
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        src = _write_pkg(tmp_path / "src", "ghost-mod")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            install_from_folder(src)
            r = await client.post(
                "/companies/me/modules/ghost-mod/delete", headers=_h(token))
            assert r.status_code == 200, r.text
            # A second delete of the same name is the never-installed case:
            # the live route reports the module itself as missing.
            r = await client.post(
                "/companies/me/modules/ghost-mod/delete", headers=_h(token))
        assert r.status_code == 404, r.text


def test_module_descriptions_use_connect_naming():
    """First-party manifest display strings name the paid tiers by their product
    names (Connect, Connect + AI); the retired Cloud naming is gone."""
    root = Path(__file__).resolve().parent.parent.parent / "default_modules"
    stale = ("Cloud subscription", "Cloud+AI", "Cloud + AI", "Cloud Backup")
    offenders = []
    for init in sorted(root.glob("*/__init__.py")):
        for n, line in enumerate(init.read_text(encoding="utf-8").splitlines(), 1):
            if '"description"' in line or '"display_name"' in line:
                if any(s in line for s in stale):
                    offenders.append(f"{init.parent.name}/__init__.py:{n}: {line.strip()}")
    assert offenders == []
