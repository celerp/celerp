"""
Verify electron/package.json is configured for stable installer filenames.

Stable names are required so that:
  - Website download links never change
  - electron-updater's latest-*.yml files reference the correct asset name
"""
import json
from pathlib import Path

_PKG = Path(__file__).parent.parent / "electron" / "package.json"


def test_stable_artifact_names():
    """Each platform must use the stable filename that the website links to."""
    pkg = json.loads(_PKG.read_text())
    build = pkg["build"]

    # mac: artifactName lives in the dedicated "dmg" section (not "mac" level,
    # because "mac" also builds a zip and a platform-level name would apply to both)
    assert build.get("dmg", {}).get("artifactName") == "Celerp-mac.dmg", (
        f"dmg.artifactName must be 'Celerp-mac.dmg', got {build.get('dmg', {}).get('artifactName')!r}. "
        "Changing this breaks website download links."
    )
    # win and linux: artifactName at the platform level
    assert build["win"].get("artifactName") == "Celerp-Setup.exe", (
        f"win.artifactName must be 'Celerp-Setup.exe', got {build['win'].get('artifactName')!r}."
    )
    assert build["linux"].get("artifactName") == "Celerp.AppImage", (
        f"linux.artifactName must be 'Celerp.AppImage', got {build['linux'].get('artifactName')!r}."
    )


def test_mac_has_zip_target():
    """Mac build must include a zip target so electron-updater can deliver updates."""
    pkg = json.loads(_PKG.read_text())
    targets = [t["target"] for t in pkg["build"]["mac"]["target"] if isinstance(t, dict)]
    assert "zip" in targets, (
        "Mac build is missing a 'zip' target. "
        "electron-updater on macOS requires a zip for the update payload (dmg is for fresh installs only)."
    )
    assert "dmg" in targets, "Mac build is missing the 'dmg' target for fresh installs."


def test_publish_allow_overwrite():
    """publish.allowOverwrite must be true so re-tagging overwrites existing release assets."""
    pkg = json.loads(_PKG.read_text())
    publish = pkg["build"].get("publish", {})
    assert publish.get("allowOverwrite") is True, (
        "publish.allowOverwrite must be true. Without it, re-tagging silently skips uploading "
        "assets that already exist on the release."
    )
