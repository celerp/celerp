# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Tests for /companies/me/modules API endpoints.

GET  /companies/me/modules
POST /companies/me/modules/{name}/enable
POST /companies/me/modules/{name}/disable

Uses the standard test client + register pattern from conftest.
"""
from __future__ import annotations

import io
import os
import shutil
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.xdist_group("modules_api")


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _register(client, role: str = "admin") -> str:
    email = f"mod-test-{uuid.uuid4().hex[:8]}@test.test"
    r = await client.post(
        "/auth/register",
        json={"company_name": "Mod Test Co", "email": email, "name": "Admin", "password": "pw123val"},
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
        r = await client.post("/companies/me/modules/celerp-labels/enable")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_disable_module_unauthenticated(self, client):
        r = await client.post("/companies/me/modules/celerp-labels/disable")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_enable_module_persists_to_settings(self, client):
        """Enabling a module adds it to company.settings enabled_modules."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        assert r.status_code == 200
        assert "celerp-labels" in r.json().get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_enable_module_that_is_not_installed_is_refused(self, client):
        """A name that is not in the module directory is not a module: nothing is
        turned on, and the company's choice is left as it was."""
        token = await _register(client)
        before = (await client.get("/companies/me", headers=_h(token))).json()["settings"]
        r = await client.post("/companies/me/modules/never-imported-module/enable", headers=_h(token))
        assert r.status_code == 404
        after = (await client.get("/companies/me", headers=_h(token))).json()["settings"]
        assert after.get("enabled_modules") == before.get("enabled_modules")

    @pytest.mark.asyncio
    async def test_disable_module_removes_from_settings(self, client):
        """Disabling a module removes it from company.settings enabled_modules."""
        token = await _register(client)
        # First enable it
        await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        # Then disable
        r = await client.post("/companies/me/modules/celerp-labels/disable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert "celerp-labels" not in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    async def test_enable_then_disable_is_idempotent(self, client):
        """Double enable is safe; enabled set is a set (no duplicates)."""
        token = await _register(client)
        await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        r = await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        enabled = data.get("enabled_modules", [])
        assert enabled.count("celerp-labels") == 1  # No duplicate

    @pytest.mark.asyncio
    async def test_disable_not_enabled_module_is_safe(self, client):
        """Disabling a module that isn't enabled returns 200, no error."""
        token = await _register(client)
        r = await client.post("/companies/me/modules/nonexistent-module/disable", headers=_h(token))
        assert r.status_code == 200
        data = r.json()
        assert "nonexistent-module" not in data.get("enabled_modules", [])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("running,expected", [(True, False), (False, True)])
    async def test_enable_asks_for_restart_only_when_the_module_is_not_running(
            self, client, running, expected):
        token = await _register(client)
        with patch("celerp.modules.loader.is_running", return_value=running), \
                patch("celerp.modules.loader.restart_would_load", return_value=True):
            r = await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token))
        assert r.status_code == 200
        assert r.json()["restart_required"] is expected

    @pytest.mark.asyncio
    async def test_disable_never_asks_for_restart(self, client):
        token = await _register(client)
        r = await client.post("/companies/me/modules/celerp-labels/disable", headers=_h(token))
        assert r.status_code == 200
        assert r.json()["restart_required"] is False

    @pytest.mark.asyncio
    async def test_company_isolation_module_settings(self, client):
        """Module settings are per-company, not global.
        
        Company A enables celerp-labels. Company B (created via POST /companies)
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

        # Company A enables celerp-labels
        r_a = await client.post("/companies/me/modules/celerp-labels/enable", headers=_h(token_a))
        assert r_a.status_code == 200, r_a.text

        # Company B should NOT see celerp-labels in its settings (it has its own settings)
        r_b_list = await client.get("/companies/me/modules", headers=_h(token_b))
        assert r_b_list.status_code == 200
        # Verify companies have separate settings by checking enabled state
        r_b_disable = await client.post(
            "/companies/me/modules/celerp-labels/disable", headers=_h(token_b)
        )
        assert r_b_disable.status_code == 200
        data_b = r_b_disable.json()
        # B's disable call should NOT return celerp-labels in the enabled list
        assert "celerp-labels" not in data_b.get("enabled_modules", [])
        # ...and B turning it off leaves A's choice alone.
        settings_a = (await client.get("/companies/me", headers=_h(token_a))).json()["settings"]
        assert "celerp-labels" in settings_a["enabled_modules"]

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

_RESERVED_NAMES = ("Names starting with 'celerp-' or 'celerp_', in any letter case, "
                   "are reserved for Marketplace modules.")


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
    async def test_scan_reports_marketplace_install(self, client, tmp_path):
        from celerp.modules.importer import install_from_zip

        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("acme-listed/__init__.py", _PKG_INIT.format(name="acme-listed", disp="Listed"))
            install_from_zip(buf.getvalue(), source="marketplace")
            (module_dir / "acme-listed" / "extra.py").write_text("x = 1\n")
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        row = next(m for m in r.json() if m["name"] == "acme-listed")
        assert row["source"] == "marketplace"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("metadata", ['{"source": "other"}', '{"source": null}', "{}", "[]",
                                         '{"source": []}', '{"source": {}}'],
                             ids=["unknown", "null", "empty", "not_an_object",
                                  "source_list", "source_object"])
    async def test_scan_reports_an_unknown_source_as_sideloaded(self, client, tmp_path, metadata):
        from celerp.modules.importer import install_from_zip
        from celerp.modules.meta import META_FILENAME

        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("acme-odd/__init__.py", _PKG_INIT.format(name="acme-odd", disp="Odd"))
            install_from_zip(buf.getvalue(), source="community")
            (module_dir / "acme-odd" / META_FILENAME).write_text(metadata)
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        row = next(m for m in r.json() if m["name"] == "acme-odd")
        assert row["source"] == "sideloaded"

    @pytest.mark.asyncio
    async def test_install_time_that_is_not_text_reads_as_unknown(self, client, tmp_path):
        from celerp.modules.importer import install_from_zip
        from celerp.modules.meta import META_FILENAME
        from fasthtml.common import to_xml
        from ui.routes.modules_page import _local_panel

        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            for name in ("acme-odd", "acme-ok"):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    zf.writestr(f"{name}/__init__.py", _PKG_INIT.format(name=name, disp=name))
                install_from_zip(buf.getvalue(), source="community")
            (module_dir / "acme-odd" / META_FILENAME).write_text(
                '{"source": "community", "installed_at": 5}')
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        rows = [m for m in r.json() if m["name"] in ("acme-odd", "acme-ok")]
        odd = next(m for m in rows if m["name"] == "acme-odd")
        assert odd["source"] == "community"
        assert isinstance(odd["installed_at"], str) and odd["installed_at"][:4].isdigit()
        assert "acme-odd" in to_xml(_local_panel(rows, lang="en"))

    @pytest.mark.asyncio
    async def test_upload_cannot_claim_marketplace_source(self, client, tmp_path):
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("acme-up/__init__.py", _PKG_INIT.format(name="acme-up", disp="Up"))
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/import", headers=_h(token),
                files={"file": ("acme-up.zip", buf.getvalue(), "application/zip")},
                data={"source": "marketplace"})
        assert r.status_code == 422, r.text
        assert not (module_dir / "acme-up").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["sideloaded", "community", None])
    async def test_upload_of_a_celerp_name_is_refused(self, client, tmp_path, source):
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        package = tmp_path / "celerp-mine"
        _write_pkg(tmp_path, "celerp-mine")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(package / "__init__.py", "celerp-mine/__init__.py")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            if source is None:
                r = await client.post("/companies/me/modules/import-path", headers=_h(token),
                                      json={"path": str(package)})
            else:
                r = await client.post(
                    "/companies/me/modules/import", headers=_h(token),
                    files={"file": ("celerp-mine.zip", buf.getvalue(), "application/zip")},
                    data={"source": source})
        assert r.status_code == 422, r.text
        assert _RESERVED_NAMES in r.json()["detail"]
        assert not (module_dir / "celerp-mine").exists()

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
    async def test_delete_default_module_refused(self, client, tmp_path):
        # Runs against a copy: pointed at the real default_modules, a locally
        # edited default would stop matching the lock and really be deleted.
        token = await _register(client)
        src = Path(__file__).parent.parent.parent / "default_modules" / "celerp-labels"
        module_dir = tmp_path / "modules"
        shutil.copytree(src, module_dir / "celerp-labels",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/celerp-labels/delete", headers=_h(token))
        assert r.status_code in (409, 422), r.text
        assert (module_dir / "celerp-labels").exists()

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


# ── module data purge (preview + drop) ────────────────────────────────────────

_PKG_INIT_PREFIX = ('PLUGIN_MANIFEST = {{"name": "{name}", "version": "1.0.0", '
                    '"display_name": "{disp}", "table_prefix": "{prefix}"}}\n')


def _write_pkg_prefix(dirpath: Path, name: str, prefix: str) -> Path:
    """A module package whose manifest declares a table_prefix (no migrations)."""
    pkg = dirpath / name
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        _PKG_INIT_PREFIX.format(name=name, disp=name.title(), prefix=prefix))
    return pkg


async def _create_tables(session, specs: list[tuple[str, int]]) -> None:
    """Create each (table_name, row_count) table with that many rows, committed
    into the test's transaction so an endpoint on the same session sees them."""
    from sqlalchemy import text
    for tname, rows in specs:
        await session.execute(text(f'CREATE TABLE "{tname}" (id serial PRIMARY KEY, v text)'))
        for i in range(rows):
            await session.execute(text(f'INSERT INTO "{tname}" (v) VALUES (:v)'), {"v": f"r{i}"})
    await session.commit()


class TestModuleDataPurge:
    @pytest.mark.asyncio
    async def test_purge_data_drops_prefixed_tables_and_keeps_module_installed(
            self, client, session, tmp_path):
        from celerp.modules.importer import install_from_folder
        from sqlalchemy import inspect as sa_inspect, text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        src = _write_pkg_prefix(tmp_path / "src", "acme-widgets", "acme_")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            install_from_folder(src)
            await _create_tables(session, [("acme_widget", 3), ("acme_log", 1)])
            # Enable then disable: disable must KEEP the data (J2 contract), so the
            # tables and rows are still present at the point purge is allowed.
            assert (await client.post(
                "/companies/me/modules/acme-widgets/enable", headers=_h(token))).status_code == 200
            assert (await client.post(
                "/companies/me/modules/acme-widgets/disable", headers=_h(token))).status_code == 200
            assert (await session.execute(
                text('SELECT count(*) FROM "acme_widget"'))).scalar_one() == 3
            assert (await session.execute(
                text('SELECT count(*) FROM "acme_log"'))).scalar_one() == 1
            # Purge drops the module's tables.
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
            assert r.status_code == 200, r.text
            names = await session.run_sync(
                lambda s: sa_inspect(s.connection()).get_table_names())
            assert "acme_widget" not in names and "acme_log" not in names
            # The module itself stays installed (folder untouched, still listed).
            assert (module_dir / "acme-widgets").exists()
            listing = await client.get("/companies/me/modules", headers=_h(token))
        assert "acme-widgets" in [m["name"] for m in listing.json()]

    @pytest.mark.asyncio
    async def test_list_modules_surfaces_table_prefix_for_purge_button(
            self, client, tmp_path):
        """A disabled module declaring table_prefix must expose it in the listing;
        the UI gates the Purge button on this field, so without it the button
        never renders in production."""
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "acme_")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.get("/companies/me/modules", headers=_h(token))
        assert r.status_code == 200, r.text
        m = next(x for x in r.json() if x["name"] == "acme-widgets")
        assert m["enabled"] is False
        assert m["table_prefix"] == "acme_"

    @pytest.mark.asyncio
    async def test_purge_data_refused_while_module_enabled(self, client, session, tmp_path):
        from sqlalchemy import text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "acme_")
        await _create_tables(session, [("acme_widget", 2)])
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            assert (await client.post(
                "/companies/me/modules/acme-widgets/enable", headers=_h(token))).status_code == 200
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        # Nothing dropped while still enabled.
        assert (await session.execute(
            text('SELECT count(*) FROM "acme_widget"'))).scalar_one() == 2

    @pytest.mark.asyncio
    async def test_purge_data_table_drop_failure_rolls_back_and_reports_error(
            self, client, session, tmp_path):
        from sqlalchemy import text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "acme_")
        # A module table plus an OUTSIDE table holding a foreign key into it: the
        # no-CASCADE drop of the module table fails and the whole purge rolls back.
        await session.execute(text('CREATE TABLE "acme_parent" (id integer PRIMARY KEY)'))
        await session.execute(text('INSERT INTO "acme_parent" (id) VALUES (1)'))
        await session.execute(text(
            'CREATE TABLE "ext_child" (id integer PRIMARY KEY, '
            'parent_id integer REFERENCES "acme_parent"(id))'))
        await session.commit()
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "depends on" in detail and "Nothing was deleted" in detail
        # Rolled back: the module table and its row survive untouched.
        assert (await session.execute(
            text('SELECT count(*) FROM "acme_parent"'))).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_purge_data_with_no_prefixed_tables_is_noop_success(
            self, client, session, tmp_path):
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "acme_")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True

    @pytest.mark.asyncio
    async def test_purge_data_refused_for_grandfathered_operator_override(
            self, client, session, tmp_path):
        from test_helpers import perm_setup
        from celerp.models.company import Company
        from celerp.services.company_lock import locked_company
        from sqlalchemy import select as sa_select
        ctx = await perm_setup(client, session)
        # Simulate the pre-change grandfathered state the raised floor still bars:
        # a stored grant of manage_company_settings down to operator written straight
        # into company.settings. The floor (admin) clamps operator out at read, so
        # the purge is still refused.
        company = (await session.execute(sa_select(Company))).scalars().first()
        company = await locked_company(session, company.id)
        settings = dict(company.settings or {})
        rg = dict(settings.get("role_grants") or {})
        rg["manage_company_settings"] = ["operator", "manager", "admin", "owner"]
        settings["role_grants"] = rg
        company.settings = settings
        await session.commit()
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "acme_")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=ctx["operator_h"])
        assert r.status_code == 403, r.text

    @pytest.mark.asyncio
    async def test_delete_keeps_module_tables_then_reimport_purge_drops_them(
            self, client, session, tmp_path):
        from celerp.modules.importer import install_from_folder
        from sqlalchemy import inspect as sa_inspect, text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        src = _write_pkg_prefix(tmp_path / "src", "acme-widgets", "acme_")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            install_from_folder(src)
            assert (module_dir / "acme-widgets").exists()
            await _create_tables(session, [("acme_widget", 4), ("acme_meta", 2)])
            # Delete removes the code and frees the name, but KEEPS the data (J3).
            r = await client.post(
                "/companies/me/modules/acme-widgets/delete", headers=_h(token))
            assert r.status_code == 200, r.text
            assert not (module_dir / "acme-widgets").exists()
            assert (await session.execute(
                text('SELECT count(*) FROM "acme_widget"'))).scalar_one() == 4
            assert (await session.execute(
                text('SELECT count(*) FROM "acme_meta"'))).scalar_one() == 2
            # The name is free: the same folder re-imports (J5, the upgrade path).
            info = install_from_folder(src)
            assert info["name"] == "acme-widgets"
            # Re-import then purge is the recovery path that drops the survivors (J6).
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
            assert r.status_code == 200, r.text
            names = await session.run_sync(
                lambda s: sa_inspect(s.connection()).get_table_names())
        assert "acme_widget" not in names and "acme_meta" not in names


class TestPurgeRechecksTablePrefix:
    """A module copied straight into MODULE_DIR never passed the install check, so
    the purge re-checks its prefix before dropping anything."""

    @pytest.mark.asyncio
    async def test_hand_copied_module_claiming_a_core_table_cannot_purge_it(
            self, client, session, tmp_path):
        from sqlalchemy import text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-grabber", "connector_")
        before = (await session.execute(text('SELECT count(*) FROM "connector_configs"'))).scalar_one()
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-grabber/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        assert "connector_configs" in r.json()["detail"]
        assert "Nothing was deleted" in r.json()["detail"]
        assert (await session.execute(
            text('SELECT count(*) FROM "connector_configs"'))).scalar_one() == before

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", ["label_", "marketplace_", "bank_"])
    async def test_hand_copied_module_claiming_a_turned_off_bundled_module_table_cannot_purge_it(
            self, client, session, tmp_path, bundled_modules_unloaded, prefix):
        from sqlalchemy import text
        table = bundled_modules_unloaded[prefix]
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-grabber", prefix)
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-grabber/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        assert table in r.json()["detail"]
        assert "Nothing was deleted" in r.json()["detail"]
        assert (await session.execute(text(f"SELECT to_regclass('{table}')"))).scalar() is not None

    @pytest.mark.asyncio
    async def test_hand_copied_module_claiming_a_core_table_without_a_model_cannot_purge_it(
            self, client, session, tmp_path):
        from sqlalchemy import text
        token = await _register(client)
        await session.execute(text(
            "CREATE TABLE IF NOT EXISTS instance_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"))
        await session.execute(text(
            "INSERT INTO instance_meta VALUES ('zz_marker', 'kept') ON CONFLICT (key) DO NOTHING"))
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-grabber", "instance_")
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-grabber/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        assert "instance_meta" in r.json()["detail"]
        assert "Nothing was deleted" in r.json()["detail"]
        assert (await session.execute(text(
            "SELECT value FROM instance_meta WHERE key = 'zz_marker'"))).scalar_one() == "kept"

    @pytest.mark.asyncio
    async def test_hand_copied_module_overlapping_another_cannot_purge_its_tables(
            self, client, session, tmp_path):
        from sqlalchemy import text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "acme_")
        _write_pkg_prefix(module_dir, "acme-sub", "acme_sub_")
        await _create_tables(session, [("acme_widget", 1), ("acme_sub_thing", 2)])
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        assert "overlaps" in r.json()["detail"]
        assert (await session.execute(
            text('SELECT count(*) FROM "acme_sub_thing"'))).scalar_one() == 2
        assert (await session.execute(
            text('SELECT count(*) FROM "acme_widget"'))).scalar_one() == 1

    @pytest.mark.asyncio
    async def test_hand_copied_module_with_a_too_short_prefix_cannot_purge(
            self, client, session, tmp_path):
        from sqlalchemy import text
        token = await _register(client)
        module_dir = tmp_path / "modules"
        module_dir.mkdir()
        _write_pkg_prefix(module_dir, "acme-widgets", "a")
        await _create_tables(session, [("acme_widget", 1)])
        with patch.dict(os.environ, {"MODULE_DIR": str(module_dir)}):
            r = await client.post(
                "/companies/me/modules/acme-widgets/purge-data", headers=_h(token))
        assert r.status_code == 409, r.text
        assert (await session.execute(
            text('SELECT count(*) FROM "acme_widget"'))).scalar_one() == 1
