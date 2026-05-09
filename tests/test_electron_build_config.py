"""
Verify electron/package.json is configured for stable installer filenames.

Stable names are required so that:
  - Website download links never change
  - electron-updater's latest-*.yml files reference the correct asset name
"""
import json
import re
from pathlib import Path

_PKG = Path(__file__).parent.parent / "electron" / "package.json"
_MAIN = Path(__file__).parent.parent / "electron" / "main.js"
_PRELOAD = Path(__file__).parent.parent / "electron" / "preload.js"


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


# ---------------------------------------------------------------------------
# main.js: updater event wiring
# ---------------------------------------------------------------------------

def _main_src() -> str:
    return _MAIN.read_text()


def test_main_emits_download_progress_ipc():
    """main.js must listen to download-progress and forward it to the renderer."""
    src = _main_src()
    assert '"download-progress"' in src or "'download-progress'" in src, (
        "main.js does not listen to the electron-updater 'download-progress' event. "
        "The progress bar will never update."
    )
    assert "download-progress" in src and "send" in src, (
        "main.js must call mainWindow.webContents.send('download-progress', ...) "
        "to forward progress events to the renderer."
    )


def test_main_emits_update_log_ipc():
    """main.js must send 'update-log' IPC messages to the renderer for the log panel."""
    src = _main_src()
    assert '"update-log"' in src or "'update-log'" in src, (
        "main.js does not emit 'update-log' IPC messages. "
        "The log panel in the update card will never receive any text."
    )


def test_main_listens_checking_for_update():
    """main.js must handle the 'checking-for-update' autoUpdater event to log it."""
    src = _main_src()
    assert "checking-for-update" in src, (
        "main.js does not handle the 'checking-for-update' autoUpdater event. "
        "The log panel will show no entry when a check begins."
    )


def test_main_kill_subprocesses_before_quit_and_install():
    """main.js must kill uiProcess/apiProcess before calling quitAndInstall.

    ShipIt (Squirrel.Mac) aborts if the app process is still alive when it tries
    to replace the bundle. Killing child processes first lets the OS reap them
    before ShipIt does its check.
    """
    src = _main_src()
    # Find the install-update handler block
    match = re.search(r'ipcMain\.on\(["\']install-update["\'].*?}\);', src, re.DOTALL)
    assert match, "ipcMain.on('install-update', ...) handler not found in main.js"
    handler = match.group(0)
    assert "uiProcess" in handler and ".kill()" in handler, (
        "install-update handler must kill uiProcess before calling quitAndInstall."
    )
    assert "apiProcess" in handler and ".kill()" in handler, (
        "install-update handler must kill apiProcess before calling quitAndInstall."
    )
    assert "quitAndInstall" in handler, (
        "install-update handler must call autoUpdater.quitAndInstall()."
    )
    # The kill must come before quitAndInstall in source order
    kill_pos = handler.index(".kill()")
    quit_pos = handler.index("quitAndInstall")
    assert kill_pos < quit_pos, (
        "uiProcess/apiProcess must be killed BEFORE quitAndInstall is called, "
        "otherwise ShipIt sees the app still running and aborts the install."
    )


# ---------------------------------------------------------------------------
# preload.js: IPC bridge completeness
# ---------------------------------------------------------------------------

def _preload_src() -> str:
    return _PRELOAD.read_text()


def test_preload_exposes_on_download_progress():
    """preload.js must expose onDownloadProgress so the renderer can show the progress bar."""
    src = _preload_src()
    assert "onDownloadProgress" in src, (
        "preload.js does not expose 'onDownloadProgress'. "
        "The renderer cannot receive download-progress events and the progress bar will never update."
    )
    assert "download-progress" in src, (
        "preload.js must listen on the 'download-progress' IPC channel in onDownloadProgress."
    )


def test_preload_exposes_on_update_log():
    """preload.js must expose onUpdateLog so the renderer can populate the log panel."""
    src = _preload_src()
    assert "onUpdateLog" in src, (
        "preload.js does not expose 'onUpdateLog'. "
        "The renderer cannot receive log lines and the log panel will stay empty."
    )
    assert "update-log" in src, (
        "preload.js must listen on the 'update-log' IPC channel in onUpdateLog."
    )


def test_preload_exposes_required_updater_api():
    """preload.js must expose the full updater API surface."""
    src = _preload_src()
    required = [
        "onUpdateAvailable",
        "onUpdateDownloaded",
        "onUpdateNotAvailable",
        "onDownloadProgress",
        "onUpdateLog",
        "checkForUpdates",
        "installUpdate",
    ]
    for name in required:
        assert name in src, (
            f"preload.js is missing '{name}'. "
            f"The renderer will throw when it tries to call window.celerp.{name}()."
        )


# ---------------------------------------------------------------------------
# shell.py: update card HTML structure
# ---------------------------------------------------------------------------

def _shell_src() -> str:
    shell = Path(__file__).parent.parent / "ui" / "components" / "shell.py"
    return shell.read_text()


def test_shell_has_progress_bar_element():
    """shell.py must render a progress bar element inside the update card."""
    src = _shell_src()
    assert "update-card__progress-bar" in src, (
        "shell.py is missing the '.update-card__progress-bar' element. "
        "The progress bar will not appear during downloads."
    )
    assert "update-card__progress-fill" in src, (
        "shell.py is missing the '.update-card__progress-fill' element. "
        "The JS cannot animate the progress bar width."
    )


def test_shell_has_log_element():
    """shell.py must render a log <pre> element inside the update card."""
    src = _shell_src()
    assert "update-card__log" in src, (
        "shell.py is missing the '.update-card__log' element. "
        "Updater log lines have nowhere to go."
    )


def test_shell_check_btn_hides_on_click():
    """The JS in shell.py must call setCheckBtn(false) when the check button is clicked."""
    src = _shell_src()
    # setCheckBtn(false) hides the button; must appear in the checkBtn click handler
    assert "setCheckBtn(false)" in src, (
        "shell.py JS does not call setCheckBtn(false) anywhere. "
        "The check button will remain visible while checking/downloading."
    )


def test_shell_check_btn_hidden_on_update_available():
    """When an update is found, the check button must be hidden (not just disabled)."""
    src = _shell_src()
    # onUpdateAvailable callback must hide the check button
    # Find the onUpdateAvailable block - look for setCheckBtn(false) near it
    oa_idx = src.find("onUpdateAvailable")
    assert oa_idx != -1, "onUpdateAvailable not found in shell.py"
    # Within 300 chars of the onUpdateAvailable callback, setCheckBtn(false) must appear
    nearby = src[oa_idx: oa_idx + 400]
    assert "setCheckBtn(false)" in nearby, (
        "onUpdateAvailable handler does not call setCheckBtn(false). "
        "The check button will remain visible while a download is in progress."
    )


def test_shell_check_btn_shown_on_not_available():
    """When no update is found, the check button must be shown again."""
    src = _shell_src()
    na_idx = src.find("onUpdateNotAvailable")
    assert na_idx != -1, "onUpdateNotAvailable not found in shell.py"
    nearby = src[na_idx: na_idx + 300]
    assert "setCheckBtn(true)" in nearby or "resetToIdle" in nearby, (
        "onUpdateNotAvailable handler does not restore the check button. "
        "The button will stay hidden after a successful 'already up to date' check."
    )


def test_shell_restart_btn_disables_on_click():
    """The restart button must disable itself when clicked to prevent double-clicks."""
    src = _shell_src()
    rb_idx = src.find("restartBtn.addEventListener")
    assert rb_idx != -1, "restartBtn click listener not found in shell.py"
    nearby = src[rb_idx: rb_idx + 300]
    assert "disabled = true" in nearby or ".disabled" in nearby, (
        "restartBtn click handler does not disable the button. "
        "Users could click it multiple times and trigger multiple install calls."
    )


def test_css_has_progress_bar_styles():
    """app.css must define styles for the progress bar elements."""
    css = (Path(__file__).parent.parent / "ui" / "static" / "app.css").read_text()
    assert ".update-card__progress-bar" in css, (
        "app.css is missing .update-card__progress-bar styles. "
        "The progress bar will be invisible."
    )
    assert ".update-card__progress-fill" in css, (
        "app.css is missing .update-card__progress-fill styles. "
        "The fill animation will not work."
    )
    assert ".update-card__log" in css, (
        "app.css is missing .update-card__log styles. "
        "The log panel will have no styling."
    )

