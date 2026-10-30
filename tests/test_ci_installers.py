"""CI never downloads a published release's files: GitHub counts each download as a
user's. The installers CI upgrades from are copies on a draft release
(scripts/ci_installers.py), filled by the release build from its own artifacts; an
app under test is kept off the public release (hide-releases).

The helper runs against a local server answering like GitHub: drafts, asset
downloads that redirect to signed storage (which refuses the API token), uploads.
The guards read every workflow and CI script, so a new download site, a new action
or a job that runs binaries before hiding the public release fails here.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).parent.parent
_SCRIPT = _ROOT / "scripts" / "ci_installers.py"
_WORKFLOWS = _ROOT / ".github" / "workflows"
_MIRROR = "ci-previous-installers"
_REPO = "/repos/celerp/celerp"


class _Store:
    def __init__(self):
        self.releases: list[dict] = []
        self.blobs: dict[int, bytes] = {}
        self.public: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.storage_auth: list[str | None] = []
        self.next_id = 100

    def release(self, tag, draft, assets=(), base=""):
        rid = self.next_id = self.next_id + 1
        rel = {"id": rid, "tag_name": tag, "draft": draft, "assets": [],
               "upload_url": f"{base}/uploads/{rid}/assets{{?name,label}}"}
        self.releases.append(rel)
        for name, data in assets:
            self.asset(rel, name, data, base)
        return rel

    def asset(self, rel, name, data, base):
        aid = self.next_id = self.next_id + 1
        self.blobs[aid] = data
        entry = {"id": aid, "name": name, "url": f"{base}{_REPO}/releases/assets/{aid}",
                 "browser_download_url": f"{base}/public/{rel['tag_name']}/{name}"}
        rel["assets"].append(entry)
        if not rel["draft"]:
            self.public[f"/public/{rel['tag_name']}/{name}"] = data
        return entry


@pytest.fixture
def github():
    store = _Store()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _bytes(self, data):
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _find_asset(self, aid):
            for rel in store.releases:
                for a in rel["assets"]:
                    if a["id"] == aid:
                        return rel, a
            return None, None

        def do_GET(self):
            store.calls.append(f"GET {self.path}")
            path = self.path.split("?", 1)[0]
            if path.startswith("/storage/"):
                store.storage_auth.append(self.headers.get("Authorization"))
                if self.headers.get("Authorization"):
                    return self._json(400, {"message": "signed URL refuses a second credential"})
                return self._bytes(store.blobs[int(path.rsplit("/", 1)[1])])
            if path.startswith("/public/"):
                return self._bytes(store.public[path]) if path in store.public else self._json(404, {})
            if path == f"{_REPO}/releases":
                return self._json(200, store.releases)
            if path == f"{_REPO}/releases/latest":
                published = [r for r in store.releases if not r["draft"]]
                return self._json(200, published[-1]) if published else self._json(404, {})
            m = re.fullmatch(rf"{_REPO}/releases/tags/(.+)", path)
            if m:
                rel = next((r for r in store.releases if r["tag_name"] == m[1] and not r["draft"]), None)
                return self._json(200, rel) if rel else self._json(404, {})
            m = re.fullmatch(rf"{_REPO}/releases/(\d+)/assets", path)
            if m:
                rel = next(r for r in store.releases if r["id"] == int(m[1]))
                return self._json(200, rel["assets"])
            m = re.fullmatch(rf"{_REPO}/releases/assets/(\d+)", path)
            if m and self.headers.get("Accept") == "application/octet-stream":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/storage/{m[1]}")
                self.end_headers()
                return
            self._json(404, {})

        def do_POST(self):
            store.calls.append(f"POST {self.path}")
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == f"{_REPO}/releases":
                body = json.loads(data)
                return self._json(201, store.release(body["tag_name"], body["draft"], base=base))
            m = re.fullmatch(r"/uploads/(\d+)/assets\?name=(.+)", self.path)
            if m:
                rel = next(r for r in store.releases if r["id"] == int(m[1]))
                if any(a["name"] == m[2] for a in rel["assets"]):
                    return self._json(422, {"message": "already_exists"})
                return self._json(201, store.asset(rel, m[2], data, base))
            self._json(404, {})

        def do_DELETE(self):
            store.calls.append(f"DELETE {self.path}")
            m = re.fullmatch(rf"{_REPO}/releases/assets/(\d+)", self.path)
            rel, asset = self._find_asset(int(m[1])) if m else (None, None)
            if asset is None:
                return self._json(404, {})
            rel["assets"].remove(asset)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store.base = f"http://127.0.0.1:{server.server_port}"
    yield store
    server.shutdown()
    server.server_close()


def _run(github, tmp_path, *args):
    env = {**os.environ, "GITHUB_API_URL": github.base, "GITHUB_REPOSITORY": "celerp/celerp",
           "GH_TOKEN": "t0ken", "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "out")}
    r = subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, r.stdout + r.stderr


def _public_reads(github):
    return [c for c in github.calls if c.startswith("GET /public/")]


def test_fetch_reads_the_mirror_copy_and_never_the_public_release(github, tmp_path):
    github.release("v2.7.0", False, [("Celerp-Setup.exe", b"public")], github.base)
    github.release(_MIRROR, True, [("v2.7.0-Celerp-Setup.exe", b"mirrored")], github.base)
    code, out = _run(github, tmp_path, "fetch", "latest", "Celerp-Setup.exe", str(tmp_path / "prev"))
    assert code == 0, out
    assert (tmp_path / "prev" / "Celerp-Setup.exe").read_bytes() == b"mirrored"
    assert (tmp_path / "out").read_text() == "tag=v2.7.0\n"
    assert _public_reads(github) == []
    # The signed storage URL is fetched without the API token, which it would refuse.
    assert github.storage_auth == [None]


def test_a_copy_the_mirror_lacks_is_an_error_naming_the_tag_with_no_public_fallback(github, tmp_path):
    github.release("v2.5.6", False, [("Celerp-Setup.exe", b"public")], github.base)
    github.release(_MIRROR, True, [("v2.7.0-Celerp-Setup.exe", b"other")], github.base)
    code, out = _run(github, tmp_path, "fetch", "v2.5.6", "Celerp-Setup.exe", str(tmp_path / "prev"))
    assert code == 1
    assert "::error::" in out and "Celerp-Setup.exe of v2.5.6" in out and "seed v2.5.6 Celerp-Setup.exe" in out
    assert _public_reads(github) == [] and not (tmp_path / "prev" / "Celerp-Setup.exe").exists()


def test_a_mirror_the_token_cannot_see_is_an_error_saying_why(github, tmp_path):
    github.release("v2.7.0", False, [("Celerp-Setup.exe", b"public")], github.base)
    code, out = _run(github, tmp_path, "fetch", "v2.7.0", "Celerp-Setup.exe", str(tmp_path / "prev"))
    assert code == 1 and f"no draft release {_MIRROR} is visible" in out and "contents: write" in out
    assert _public_reads(github) == []


@pytest.mark.parametrize("case", ["published", "two drafts"])
def test_the_mirror_is_exactly_one_draft(github, tmp_path, case):
    """A published mirror would put the copies on the public counter; with two drafts
    the copy read would depend on list order."""
    copy = [("v2.7.0-Celerp-Setup.exe", b"x")]
    github.release(_MIRROR, case != "published", copy, github.base)
    if case == "two drafts":
        github.release(_MIRROR, True, copy, github.base)
    code, out = _run(github, tmp_path, "fetch", "v2.7.0", "Celerp-Setup.exe", str(tmp_path / "prev"))
    assert code == 1 and ("must stay a draft" if case == "published" else "keep the one draft") in out, out
    assert not (tmp_path / "prev" / "Celerp-Setup.exe").exists()


def test_put_creates_the_draft_and_replaces_a_copy_of_the_same_name(github, tmp_path):
    exe, app = tmp_path / "Celerp-Setup.exe", tmp_path / "Celerp.AppImage"
    exe.write_bytes(b"exe-1")
    app.write_bytes(b"app-1")
    assert _run(github, tmp_path, "put", "v2.8.0", str(exe), str(app))[0] == 0
    (mirror,) = github.releases
    assert mirror["tag_name"] == _MIRROR and mirror["draft"] is True
    exe.write_bytes(b"exe-2")
    code, out = _run(github, tmp_path, "put", "v2.8.0", str(exe))
    assert code == 0, out
    copies = {a["name"]: github.blobs[a["id"]] for a in mirror["assets"]}
    assert copies == {"v2.8.0-Celerp-Setup.exe": b"exe-2", "v2.8.0-Celerp.AppImage": b"app-1"}
    assert _public_reads(github) == []


@pytest.mark.parametrize("file", ["Celerp-mac.dmg", "missing.exe"])
def test_put_takes_only_a_built_installer_ci_upgrades_from(github, tmp_path, file):
    if file != "missing.exe":
        (tmp_path / file).write_bytes(b"dmg")
    code, out = _run(github, tmp_path, "put", "v2.8.0", str(tmp_path / file))
    assert code == 1 and "::error::" in out
    assert not any(a for r in github.releases for a in r["assets"])


def test_seed_reads_each_public_installer_once(github, tmp_path):
    github.release("v2.5.6", False, [("Celerp-Setup.exe", b"exe"), ("Celerp.AppImage", b"app")], github.base)
    code, out = _run(github, tmp_path, "seed", "latest", "Celerp-Setup.exe", "Celerp.AppImage")
    assert code == 0, out
    assert sorted(_public_reads(github)) == ["GET /public/v2.5.6/Celerp-Setup.exe", "GET /public/v2.5.6/Celerp.AppImage"]
    code, out = _run(github, tmp_path, "seed", "v2.5.6", "Celerp-Setup.exe", "Celerp.AppImage")
    assert code == 0 and out.count("is already mirrored") == 2
    assert len(_public_reads(github)) == 2
    mirror = next(r for r in github.releases if r["tag_name"] == _MIRROR)
    assert {a["name"]: github.blobs[a["id"]] for a in mirror["assets"]} == {
        "v2.5.6-Celerp-Setup.exe": b"exe", "v2.5.6-Celerp.AppImage": b"app"}


# ── Guards over every workflow and CI script ────────────────────────────────

# Everything CI runs: the workflows, the scripts they call, the desktop build and the test
# suite. test_the_scan_covers_every_file_a_workflow_runs keeps this list whole.
_CI_ROOTS = (".github", "scripts", "electron", "tests")
_CI_SUFFIXES = {".yml", ".yaml", ".py", ".sh", ".ps1", ".js", ".mjs", ".cjs"}


def _ci_files():
    for folder in _CI_ROOTS:
        for path in sorted((_ROOT / folder).rglob("*")):
            if path.is_file() and path.suffix in _CI_SUFFIXES and "node_modules" not in path.parts:
                yield path


# A download of one of this repository's release files, in any form a workflow or
# script can make one, in any letter case (HTTP headers and URLs paths are matched as
# typed by any tool). Another project's release (the Python build, OpenSSL), named by its
# owner on github.com, is that project's counter, not ours.
_DOWNLOAD = re.compile(r"gh\s+release\s+download|browser_download_url|Accept\W{0,4}application/octet-stream", re.I)
_RELEASE_FILE = re.compile(r"releases/(?:latest/)?download", re.I)
_OTHER_PROJECT = re.compile(r"github\.com/(?!celerp/)[\w.-]+/[\w.-]+/$", re.I)
# A command continued on the next line (shell \, PowerShell `), or a YAML folded block, is
# one command: the scan reads the text as one line with every line break and indent folded.
_CONTINUATION = re.compile(r"[\\`]\r?\n")


def _logical(text: str) -> str:
    return re.sub(r"\s+", " ", _CONTINUATION.sub(" ", text))


def _downloads_ours(text: str) -> list[str]:
    """Each download of this repository's release files in ``text``, as the text around it."""
    text = _logical(text)
    found = [m for m in _DOWNLOAD.finditer(text)]
    found += [m for m in _RELEASE_FILE.finditer(text) if not _OTHER_PROJECT.search(text[:m.start()])]
    return [text[max(0, m.start() - 60):m.end() + 20] for m in found]


def test_no_ci_file_downloads_a_release_file_outside_the_mirror_helper():
    guard = Path(__file__).resolve()
    found = [f"{path.relative_to(_ROOT)}: {hit}" for path in _ci_files() if path not in (_SCRIPT, guard)
             for hit in _downloads_ours(path.read_text(encoding="utf-8"))]
    assert found == []
    # Other projects' release downloads stay: the scan passes them, not skips them.
    assert any("/releases/download" in p.read_text(encoding="utf-8") for p in _ci_files())


def test_the_scan_covers_every_file_a_workflow_runs():
    """Every script a workflow names (run steps, working directories) lies under _CI_ROOTS."""
    named = set()
    for wf in (_ROOT / ".github" / "workflows").glob("*.y*ml"):
        named |= set(re.findall(r"[\w./-]+\.(?:py|sh|ps1|js|mjs|cjs)\b", wf.read_text(encoding="utf-8")))
    scanned = set(_ci_files())
    outside = []
    for name in sorted(named):
        direct = (_ROOT / name.lstrip("./")).resolve()
        candidates = [direct] if direct.is_file() else [
            p for p in _ROOT.rglob(Path(name).name) if "node_modules" not in p.parts and ".git" not in p.parts]
        outside += [f"{name} -> {p.relative_to(_ROOT)}" for p in candidates if p.resolve() not in scanned]
    assert named and outside == []


def test_the_guard_pattern_catches_every_shape_of_a_release_download():
    shapes = [
        'gh release download "$PREVIOUS" --repo "$GITHUB_REPOSITORY" --pattern Celerp-Setup.exe',
        "gh  release  download v2.5.6",
        "gh release \\\n  download v2.5.6",
        "gh release `\n  download v2.5.6",
        "run: >\n  gh release\n  download v2.5.6\n",
        "GH Release Download v2.5.6",
        "curl -fL https://github.com/celerp/celerp/releases/download/v2.5.6/Celerp-Setup.exe",
        "curl -fL https://github.com/Celerp/Celerp/Releases/Download/v2.5.6/Celerp-Setup.exe",
        "curl -fL https://github.com/celerp/celerp/releases/latest/download/Celerp-Setup.exe",
        'curl -L "https://github.com/$GITHUB_REPOSITORY/releases/download/$TAG/Celerp.AppImage"',
        "Invoke-WebRequest https://github.com/${{ github.repository }}/releases/download/x/y",
        "Invoke-WebRequest \"https://github.com/$env:GITHUB_REPOSITORY/releases/download/$tag/Celerp-Setup.exe\"",
        'url = f"{base}/releases/download/{tag}/{name}"',
        "a['browser_download_url']",
        "a['BROWSER_DOWNLOAD_URL']",
        '-H "Accept: application/octet-stream" "$ASSET_URL"',
        '-Headers @{ Accept = "application/octet-stream" }',
        'headers={"Accept": "application/octet-stream"}',
        'headers={"accept": "application/octet-stream"}',
        "gh api repos/celerp/celerp/releases/assets/123 -H accept:application/octet-stream",
        "gh api repos/celerp/celerp/releases/assets/123 \\\n  -H 'Accept: application/octet-stream'",
    ]
    assert [s for s in shapes if not _downloads_ours(s)] == []
    # Another project's release, and an upload's content type, are not ours to count.
    assert _downloads_ours("curl -fL https://github.com/indygreg/python-build-standalone/releases/download/x/y") == []
    assert _downloads_ours('files = {"file": (name, data, "application/octet-stream")}') == []


def test_the_helper_reads_the_public_release_only_to_seed():
    source = _SCRIPT.read_text()
    seed = source.split("\ndef seed(", 1)[1].split("\ndef ", 1)[0]
    assert source.count("browser_download_url") == 1 and "browser_download_url" in seed


def _workflows():
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        yield path.name, yaml.safe_load(path.read_text())


# Every action a workflow uses. An action that downloads this repository's release
# files would count on the public counter, so a new one is read before it is added.
_ACTIONS = {
    "actions/checkout", "actions/setup-node", "actions/setup-python", "actions/cache",
    "actions/upload-artifact", "actions/download-artifact", "pnpm/action-setup",
    "pypa/gh-action-pypi-publish",
    # Runs the script written in the workflow, which the download scan above reads.
    "actions/github-script",
    # Publishes each stable release to winget. The winget manifest names the public
    # installer users download, so its hash is taken from that file, and the winget
    # validation fetches it again: a few downloads per stable release, outside this
    # repository's CI copies.
    "vedantmgoyal2009/winget-releaser",
}


def test_every_action_a_workflow_uses_is_one_read_for_release_downloads():
    used = set()
    for _, wf in _workflows():
        for job in wf["jobs"].values():
            if "uses" in job:
                used.add(job["uses"])
            used.update(s["uses"] for s in job.get("steps", []) if "uses" in s)
    third_party = {u.split("@", 1)[0] for u in used if not u.startswith("./")}
    assert third_party - _ACTIONS == set()
    assert {u for u in used if u.startswith("./")} == {"./.github/workflows/packaged-upgrade-smoke.yml"}


def _obtains_binaries(step: dict) -> bool:
    if step.get("uses", "").startswith("actions/download-artifact"):
        return str(step.get("with", {}).get("name", "")).startswith("binaries-")
    run = step.get("run", "")
    return bool(re.search(r"(?m)^\s*(?:pnpm exec electron-builder|pnpm run build:|\$\{\{ matrix\.build_cmd \}\})", run)
                or re.search(r"gh run download|ci_installers\.py fetch", run))


def _hides(step: dict) -> bool:
    return "ci_installers.py hide-releases" in step.get("run", "")


def _binary_jobs():
    for name, wf in _workflows():
        for job_name, job in wf["jobs"].items():
            steps = job.get("steps", [])
            if any(_obtains_binaries(s) for s in steps):
                yield f"{name}:{job_name}", steps


def test_every_job_with_binaries_hides_the_public_release_before_running_them():
    """An installed release older than the latest one downloads the latest installer by
    itself on its first window load, and every copy reads latest*.yml. So the moment a
    job's binaries exist, the next step makes github.com unreachable; nothing runs in
    between. A job that only copies them to the mirror runs none."""
    checked = set()
    for where, steps in _binary_jobs():
        last = max(i for i, s in enumerate(steps) if _obtains_binaries(s))
        rest = steps[last + 1:]
        if rest and all("ci_installers.py put" in s.get("run", "") for s in rest):
            assert not any(_hides(s) for s in steps), where
            continue
        assert [i for i, s in enumerate(steps) if _hides(s)] == [last + 1], where
        checked.add(where)
    assert checked == {"build.yml:build", "build.yml:win-names", "packaged-upgrade-smoke.yml:upgrade"}


def test_the_hide_step_runs_on_every_runner_its_job_uses():
    for where, steps in _binary_jobs():
        for step in filter(_hides, steps):
            assert "if" not in step, where


def test_every_previous_installer_ci_installs_comes_from_the_mirror():
    wf = dict(_workflows())
    names = wf["build.yml"]["jobs"]["win-names"]["steps"]
    fetch = next(s for s in names if "ci_installers.py fetch" in s.get("run", ""))
    assert fetch["run"] == 'python scripts/ci_installers.py fetch "${{ matrix.previous }}" Celerp-Setup.exe "$env:RUNNER_TEMP\\previous"'
    # Reading a draft needs push access.
    assert wf["build.yml"]["permissions"] == {"contents": "write"} and "permissions" not in wf["build.yml"]["jobs"]["win-names"]
    upgrade = wf["packaged-upgrade-smoke.yml"]
    runs = "\n".join(s.get("run", "") for s in upgrade["jobs"]["upgrade"]["steps"])
    assert 'scripts/ci_installers.py fetch "$PREVIOUS" "${{ matrix.asset }}" "$RUNNER_TEMP/previous"' in runs
    assert upgrade["permissions"]["contents"] == "write"
    assert wf["build.yml"]["jobs"]["upgrade-smoke"]["permissions"]["contents"] == "write"
    rows = wf["build.yml"]["jobs"]["win-names"]["strategy"]["matrix"]["include"]
    assert {r["previous"] for r in rows} == {"v2.5.6", "latest"}


def test_each_published_release_mirrors_its_own_built_installers():
    jobs = dict(_workflows())["build.yml"]["jobs"]
    mirror = jobs["mirror-installers"]
    assert mirror["needs"] == ["publish-release"]
    assert mirror["if"] == "${{ success() && startsWith(github.ref, 'refs/tags/v') }}"
    assert mirror["permissions"] == {"contents": "write"}
    downloads = [s["with"]["name"] for s in mirror["steps"] if s.get("uses", "").startswith("actions/download-artifact")]
    assert downloads == ["binaries-windows-latest", "binaries-ubuntu-latest"]
    put = mirror["steps"][-1]["run"]
    assert 'ci_installers.py put "$GITHUB_REF_NAME"' in put
    assert "/Celerp-Setup.exe" in put and "/Celerp.AppImage" in put
    # A failed copy opens the release-failure issue, which tells it from an unpublished release.
    notify = jobs["notify-release-failure"]
    assert "mirror-installers" in notify["needs"]
    issue = notify["steps"][0]
    assert issue["env"]["PUBLISHED"] == "${{ needs.publish-release.result == 'success' }}"
    assert "is published, but copying its installers to the CI mirror draft failed" in issue["run"]


def test_every_job_that_writes_the_mirror_waits_for_the_others():
    """put and seed read the mirror draft and then change it; two at once could make a second
    draft. Every job that runs either one shares one queue that cancels nothing."""
    writers = {}
    for name, wf in _workflows():
        for job_id, job in (wf.get("jobs") or {}).items():
            runs = " ".join(str(step.get("run", "")) for step in job.get("steps") or [])
            if re.search(r"ci_installers\.py\s+(?:put|seed)\b", runs):
                writers[f"{name}:{job_id}"] = job.get("concurrency")
    assert set(writers) == {"build.yml:mirror-installers", "ci-installers.yml:seed"}, writers
    assert all(c == {"group": "ci-installers-mirror", "cancel-in-progress": False, "queue": "max"}
               for c in writers.values()), writers


def test_the_seed_workflow_runs_only_on_demand():
    wf = dict(_workflows())["ci-installers.yml"]
    assert set(wf[True]) == {"workflow_dispatch"}
    (job,) = wf["jobs"].values()
    assert job["steps"][-1]["run"] == 'python3 scripts/ci_installers.py seed "$TAG" $ASSETS'
