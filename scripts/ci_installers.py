# Copyright (c) 2026 Noah Severs
# SPDX-License-Identifier: LicenseRef-Proprietary
"""Released installers for CI, kept off the public release counters.

GitHub counts every download of a published release's asset as a user download, so
CI never downloads one. The installers CI upgrades from are copies held on the draft
release MIRROR as <tag>-<asset> (Celerp-Setup.exe of v2.7.0 is
v2.7.0-Celerp-Setup.exe). A draft is never shown to the public and its downloads
are not a published release's.

    python scripts/ci_installers.py fetch <tag|latest> <asset> <dir>
    python scripts/ci_installers.py put <tag> <file>...
    python scripts/ci_installers.py seed <tag|latest> <asset>...
    python scripts/ci_installers.py hide-releases

fetch: writes the mirrored <asset> of <tag> to <dir>/<asset> and prints the tag
("latest" is the latest published release, read from its metadata). A copy the
mirror does not hold is an error naming the tag; there is no fallback to the
public release.

put: copies files this run built to the mirror under <tag>, replacing a copy of
the same name. The release build puts each release's installers once the release
is published, from its own build artifacts.

seed: mirrors <asset> of a published release by downloading it from the public
release, the one place that does: each seeded file counts once on the public
counter. A copy already mirrored is left alone, so a seed is never counted twice.

hide-releases: makes github.com unreachable on this runner (hosts file), so no
app under test reaches the public release: an installed release older than the
latest one downloads the latest installer by itself on its first window load
(electron-updater), and every copy reads latest*.yml. Run it once the job's
binaries are in hand, before anything installs or opens them.

Reading a draft needs push access, so a job that fetches needs contents: write.
Reads GITHUB_API_URL, GITHUB_REPOSITORY and GH_TOKEN. Exit status: 0 done, 1 not.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from release_gate import Refused, api_get

MIRROR = "ci-previous-installers"
ASSETS = ("Celerp-Setup.exe", "Celerp.AppImage")
HIDDEN_HOST = "github.com"


def _repo() -> str:
    return f"{os.environ['GITHUB_API_URL']}/repos/{os.environ['GITHUB_REPOSITORY']}"


def _headers(accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {"Authorization": f"token {os.environ['GH_TOKEN']}", "Accept": accept}


def _send(request: urllib.request.Request, what: str) -> object:
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raise Refused(f"GitHub API returned HTTP {e.code} for {what}") from e
    except (urllib.error.URLError, OSError) as e:
        raise Refused(f"GitHub API request failed for {what}: {e}") from e
    return json.loads(raw) if raw else None


def _asset_name(tag: str, asset: str) -> str:
    if asset not in ASSETS:
        raise Refused(f"{asset} is not an installer CI upgrades from ({', '.join(ASSETS)})")
    return f"{tag}-{asset}"


def resolve(tag: str) -> str:
    """The tag itself, or for "latest" the latest published release's tag."""
    if tag != "latest":
        return tag
    body, _ = api_get(f"{_repo()}/releases/latest")
    if not isinstance(body, dict) or not body.get("tag_name"):
        raise Refused("GitHub API returned no latest release")
    return body["tag_name"]


def mirror(create: bool = False) -> dict | None:
    """The mirror draft release, created when asked and absent."""
    url, found = f"{_repo()}/releases?per_page=100", []
    while url:
        body, url = api_get(url)
        if not isinstance(body, list):
            raise Refused("GitHub API returned no release list")
        found += [r for r in body if r.get("tag_name") == MIRROR]
    if len(found) > 1:
        raise Refused(f"{len(found)} releases are tagged {MIRROR}; keep the one draft")
    if found and not found[0].get("draft"):
        raise Refused(f"release {MIRROR} is published; it must stay a draft")
    if found or not create:
        return found[0] if found else None
    body = json.dumps({"tag_name": MIRROR, "name": "CI previous installers", "draft": True,
                       "body": "Copies of released installers for CI upgrade tests (scripts/ci_installers.py)."})
    return _send(urllib.request.Request(f"{_repo()}/releases", data=body.encode(), method="POST",
                                        headers=_headers()), f"creating {MIRROR}")


def _assets(release: dict) -> dict[str, dict]:
    url, assets = f"{_repo()}/releases/{release['id']}/assets?per_page=100", {}
    while url:
        body, url = api_get(url)
        if not isinstance(body, list):
            raise Refused(f"GitHub API returned no asset list for {MIRROR}")
        assets.update((a["name"], a) for a in body)
    return assets


class _Keep(urllib.request.HTTPRedirectHandler):
    """Reports a redirect instead of following it: the storage URL GitHub redirects to
    is signed and refuses the API token."""

    def redirect_request(self, *args, **kwargs):
        return None


def _download(url: str, headers: dict[str, str], dest: Path) -> None:
    opener = urllib.request.build_opener(_Keep)
    try:
        response = opener.open(urllib.request.Request(url, headers=headers), timeout=600)
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 303, 307, 308):
            raise Refused(f"download of {dest.name} returned HTTP {e.code}") from e
        response = urllib.request.urlopen(e.headers["Location"], timeout=600)
    except (urllib.error.URLError, OSError) as e:
        raise Refused(f"download of {dest.name} failed: {e}") from e
    part = dest.with_name(dest.name + ".part")
    with response, open(part, "wb") as out:
        shutil.copyfileobj(response, out)
    if part.stat().st_size == 0:
        part.unlink()
        raise Refused(f"download of {dest.name} was empty")
    part.replace(dest)


def fetch(tag: str, asset: str, folder: str) -> str:
    tag = resolve(tag)
    name = _asset_name(tag, asset)
    release = mirror()
    if release is None:
        raise Refused(f"no draft release {MIRROR} is visible: it holds the installers CI upgrades from "
                      "(reading a draft needs contents: write, which a pull request from a fork does not get)")
    found = _assets(release).get(name)
    if found is None:
        raise Refused(f"the {MIRROR} draft holds no {asset} of {tag}: mirror it with "
                      f"`python scripts/ci_installers.py seed {tag} {asset}`")
    dest = Path(folder) / asset
    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(found["url"], _headers("application/octet-stream"), dest)
    return tag


def _upload(release: dict, existing: dict[str, dict], name: str, path: Path) -> None:
    if name in existing:
        _send(urllib.request.Request(existing[name]["url"], method="DELETE", headers=_headers()),
              f"replacing {name}")
    upload = release["upload_url"].split("{", 1)[0] + f"?name={quote(name)}"
    with open(path, "rb") as data:
        request = urllib.request.Request(upload, data=data, method="POST", headers={
            **_headers(), "Content-Type": "application/octet-stream", "Content-Length": str(path.stat().st_size)})
        _send(request, f"uploading {name}")
    print(f"mirrored {path.name} as {name}")


def put(tag: str, files: list[str]) -> None:
    release = mirror(create=True)
    existing = _assets(release)
    for file in files:
        path = Path(file)
        if not path.is_file() or path.stat().st_size == 0:
            raise Refused(f"{file} is not a built installer")
        _upload(release, existing, _asset_name(tag, path.name), path)


def seed(tag: str, assets: list[str]) -> None:
    tag = resolve(tag)
    names = {asset: _asset_name(tag, asset) for asset in assets}
    release = mirror(create=True)
    existing = _assets(release)
    body, _ = api_get(f"{_repo()}/releases/tags/{quote(tag)}")
    public = {a["name"]: a for a in body.get("assets", [])} if isinstance(body, dict) else {}
    work = Path(os.environ.get("RUNNER_TEMP") or ".") / "ci-installers-seed"
    for asset, name in names.items():
        if name in existing:
            print(f"{name} is already mirrored")
            continue
        if asset not in public:
            raise Refused(f"release {tag} has no {asset}")
        dest = work / asset
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The one public download: counted once, then CI reads the copy.
        _download(public[asset]["browser_download_url"], {}, dest)
        _upload(release, existing, name, dest)
        dest.unlink()


def hide_releases() -> None:
    lines = f"\n0.0.0.0 {HIDDEN_HOST}\n:: {HIDDEN_HOST}\n"
    if os.name == "nt":
        hosts = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts"
        with open(hosts, "a", encoding="ascii") as f:
            f.write(lines)
        subprocess.run(["ipconfig", "/flushdns"], check=True, capture_output=True)
    else:
        subprocess.run(["sudo", "tee", "-a", "/etc/hosts"], input=lines.encode(), check=True, capture_output=True)
        if sys.platform == "darwin":
            subprocess.run(["sudo", "dscacheutil", "-flushcache"], check=True)
            subprocess.run(["sudo", "killall", "-HUP", "mDNSResponder"], check=True)
    try:
        urllib.request.urlopen(f"https://{HIDDEN_HOST}/{os.environ['GITHUB_REPOSITORY']}/releases/latest", timeout=15)
    except urllib.error.HTTPError as e:
        raise Refused(f"{HIDDEN_HOST} still answers (HTTP {e.code}) after the hosts entry") from e
    except (urllib.error.URLError, OSError):
        print(f"{HIDDEN_HOST} is unreachable on this runner: no app under test reaches the public release")
        return
    raise Refused(f"{HIDDEN_HOST} still answers after the hosts entry")


def main() -> int:
    args = sys.argv[1:]
    try:
        if args[:1] == ["fetch"] and len(args) == 4:
            tag = fetch(*args[1:])
            print(f"fetched {args[2]} of {tag} from the {MIRROR} draft")
            if os.environ.get("GITHUB_OUTPUT"):
                with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                    out.write(f"tag={tag}\n")
        elif args[:1] == ["put"] and len(args) >= 3:
            put(args[1], args[2:])
        elif args[:1] == ["seed"] and len(args) >= 3:
            seed(args[1], args[2:])
        elif args == ["hide-releases"]:
            hide_releases()
        else:
            print(__doc__)
            return 1
    except Refused as e:
        print(f"::error::{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
