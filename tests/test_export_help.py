# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""scripts/export_help.py writes the help bundle the release attaches as help.json, and
only when every help check and bundle rule passes; the release job exports it from a
full clone, checks its version and attaches it beside openapi.json and importers.json."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from celerp.help import assemble, ledger
from celerp.help.bundle import bundle_problems
from celerp.help.registry import get_index

_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("export_help", _ROOT / "scripts" / "export_help.py")
export_help = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_help)

DATE = "2026-11-02"


@pytest.fixture(autouse=True)
def _no_site(monkeypatch):
    monkeypatch.delenv("CELERP_SITE", raising=False)


def _git(log: str, shallow: str = "false"):
    real = assemble._git

    def fake(*args):
        if args[0] == "rev-parse" and args[1] == "--is-shallow-repository":
            return shallow
        if args[0] == "log":
            return log
        return real(*args)
    return fake


def test_shipped_help_exports(tmp_path):
    out = tmp_path / "help.json"
    bundle = export_help.export(out, dates=lambda path: DATE)
    assert json.loads(out.read_text(encoding="utf-8")) == bundle
    assert bundle_problems(bundle) == []
    shipped = {p.id for p in get_index().pages.values() if p.source in ("core", "first_party")}
    assert {t["id"] for t in bundle["topics"]} == shipped == set(ledger.ledger()["topics"])
    assert len(shipped) == 79


def test_export_help_module_without_topic(tmp_path):
    bundle = export_help.export(tmp_path / "help.json", dates=lambda path: DATE)
    assert {t["module"] for t in bundle["topics"]} == {m["name"] for m in bundle["modules"]}


@pytest.mark.parametrize("log, shallow, named", [
    ("", "false", "has no commit"),
    ("2026-11-02", "true", "shallow"),
], ids=["git_date_empty", "shallow_clone"])
def test_export_help_git_history(tmp_path, monkeypatch, log, shallow, named):
    monkeypatch.setattr(assemble, "_git", _git(log, shallow))
    out = tmp_path / "help.json"
    with pytest.raises(SystemExit) as e:
        export_help.export(out)
    assert named in str(e.value) and not out.exists()


def test_export_help_unwritable(tmp_path):
    out = tmp_path / "missing" / "help.json"
    with pytest.raises(OSError):
        export_help.export(out, dates=lambda path: DATE)
    assert not out.parent.exists() or list(out.parent.iterdir()) == []
    assert [p.name for p in tmp_path.iterdir()] in ([], ["missing"])


def test_export_help_problems_block_write(tmp_path, monkeypatch):
    bad = json.loads(ledger.LEDGER_FILE.read_text(encoding="utf-8"))
    dropped = sorted(bad["topics"])[0]
    del bad["topics"][dropped]
    monkeypatch.setattr(ledger, "LEDGER_FILE", tmp_path / "ledger.json")
    ledger.write_data(ledger.LEDGER_FILE, bad)
    out = tmp_path / "help.json"
    with pytest.raises(SystemExit) as e:
        export_help.export(out, dates=lambda path: "not a date")
    message = str(e.value)
    assert f"shipped page {dropped} has no topics entry" in message
    assert "help.json: V6 " in message and "updated must be a real YYYY-MM-DD date" in message
    assert not out.exists()


def _job() -> dict:
    return yaml.safe_load((_ROOT / ".github" / "workflows" / "build.yml").read_text())["jobs"]["openapi-asset"]


def _step(job: dict, name: str) -> dict:
    return next(s for s in job["steps"] if s.get("name") == name)


def test_check_build_yml_help_asset_export_step():
    job = _job()
    checkout = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout")
                    and "repository" not in s.get("with", {}))
    assert checkout["with"]["fetch-depth"] == 0
    names = [s.get("name") for s in job["steps"]]
    export = _step(job, "Export the help bundle")
    assert export["run"].strip() == "python scripts/export_help.py --out help.json"
    assert names.index("Export the help bundle") == names.index("Export the importer list") + 1
    check = _step(job, "Verify the release version of the assets")["run"]
    assert 'open("help.json"' in check and '("Help", help_bundle["version"])' in check


def test_check_build_yml_help_asset_site_checkout():
    job = _job()
    site = next(s for s in job["steps"] if s.get("with", {}).get("repository"))
    assert site["with"] == {"repository": "ngsevers/celerp-cloud", "path": "site",
                            "token": "${{ secrets.SITE_READ_TOKEN }}"}
    assert "continue-on-error" not in site and "if" not in site
    assert job["env"]["CELERP_SITE"] == "${{ github.workspace }}/site"
    names = [s.get("name") for s in job["steps"]]
    assert names.index(site.get("name")) < names.index("Export the help bundle")


_CURL = r"""#!/bin/sh
echo "$*" >> "$CURL_LOG"
case "$*" in
  *http_code*name=$FAIL_ASSET*) printf '500' ;;
  *http_code*) printf '201' ;;
  *releases\?per_page*) printf '[{"id": 7, "tag_name": "v9.9.9"}]' ;;
  *assets\?per_page*) printf '[]' ;;
esac
"""


@pytest.mark.parametrize("fail", ["none", "help.json"])
def test_check_build_yml_help_asset_attach_loop(tmp_path, fail):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(_CURL)
    (bin_dir / "curl").chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(_step(_job(), "Attach the release assets to the draft release")["run"])
    log = tmp_path / "curl.log"
    run = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
                         env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "CURL_LOG": str(log),
                              "GH_TOKEN": "t", "GITHUB_REF_NAME": "v9.9.9", "FAIL_ASSET": fail},
                         capture_output=True, text=True, cwd=tmp_path)
    uploads = [line for line in log.read_text().splitlines() if "uploads.github.com" in line]
    if fail == "none":
        assert run.returncode == 0, run.stderr
        assert [u.rsplit("name=", 1)[1] for u in uploads] == ["openapi.json", "importers.json", "help.json"]
    else:
        assert run.returncode == 1 and "help.json upload returned HTTP 500" in run.stderr
