"""
Verify electron/package.json is configured for stable installer filenames.

Stable names are required so that:
  - Website download links never change
  - electron-updater's latest-*.yml files reference the correct asset name
"""
import json
from pathlib import Path

_PKG = Path(__file__).parent.parent / "electron" / "package.json"


def _artifact_names() -> dict[str, str]:
    """Return {platform: artifactName} from the build config."""
    pkg = json.loads(_PKG.read_text())
    result: dict[str, str] = {}
    for platform in ("mac", "win", "linux"):
        section = pkg["build"][platform]
        # artifactName lives at the platform level (not inside target[])
        if "artifactName" in section:
            result[platform] = section["artifactName"]
    return result


def test_stable_artifact_names():
    """Each platform must use the stable filename that the website links to."""
    names = _artifact_names()
    assert names.get("mac") == "Celerp-mac.dmg", (
        f"Mac artifactName must be 'Celerp-mac.dmg', got {names.get('mac')!r}. "
        "Changing this breaks website download links and electron-updater YML references."
    )
    assert names.get("win") == "Celerp-Setup.exe", (
        f"Win artifactName must be 'Celerp-Setup.exe', got {names.get('win')!r}."
    )
    assert names.get("linux") == "Celerp.AppImage", (
        f"Linux artifactName must be 'Celerp.AppImage', got {names.get('linux')!r}."
    )


def test_all_platforms_have_artifact_name():
    """Every platform must declare an explicit artifactName at the platform level."""
    pkg = json.loads(_PKG.read_text())
    for platform in ("mac", "win", "linux"):
        section = pkg["build"][platform]
        assert "artifactName" in section, (
            f"Platform '{platform}' is missing artifactName at the platform level. "
            "electron-builder will fall back to a versioned name, breaking stable links."
        )
