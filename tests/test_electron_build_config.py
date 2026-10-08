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
_MAIN = Path(__file__).parent.parent / "electron" / "app-main.js"


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
    # win: artifactName at the platform level (nsis is the only windows target)
    assert build["win"].get("artifactName") == "Celerp-Setup.exe", (
        f"win.artifactName must be 'Celerp-Setup.exe', got {build['win'].get('artifactName')!r}."
    )
    # linux builds two targets (AppImage + deb), so like mac the names live in
    # the dedicated per-target sections - a linux-level artifactName would name
    # both artifacts identically.
    assert build.get("appImage", {}).get("artifactName") == "Celerp.AppImage", (
        f"appImage.artifactName must be 'Celerp.AppImage', got {build.get('appImage', {}).get('artifactName')!r}. "
        "Changing this breaks website download links and latest-linux.yml."
    )
    assert build.get("deb", {}).get("artifactName") == "Celerp.deb", (
        f"deb.artifactName must be 'Celerp.deb', got {build.get('deb', {}).get('artifactName')!r}. "
        "Changing this breaks website download links."
    )
    assert build["linux"].get("artifactName") is None, (
        "linux.artifactName must not be set: it would apply to BOTH the AppImage "
        "and the deb, naming them identically. Use appImage/deb sections instead."
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
# main.js: installing a downloaded update
# ---------------------------------------------------------------------------

def _main_src() -> str:
    return _MAIN.read_text()


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


# ---------------------------------------------------------------------------
# Additional regression tests for Copilot review fixes
# ---------------------------------------------------------------------------

def test_main_initial_check_deferred_until_did_finish_load():
    """The initial autoUpdater.checkForUpdates() must fire after did-finish-load.

    Firing immediately at app-ready risks emitting IPC events before the renderer
    has registered its ipcRenderer.on(...) handlers, silently dropping them.
    """
    src = _main_src()
    assert "did-finish-load" in src, (
        "main.js does not wait for 'did-finish-load' before calling the initial "
        "checkForUpdates(). Events emitted before the renderer loads are silently dropped."
    )
    # The initial checkForUpdates call must be inside a did-finish-load callback
    finish_load_idx = src.find("did-finish-load")
    check_idx = src.find("checkForUpdates", finish_load_idx)
    assert check_idx != -1, (
        "checkForUpdates() is not called inside the did-finish-load handler. "
        "Initial update checks may fire before the renderer is ready."
    )


def test_build_yml_has_prepare_release_job():
    """build.yml must have a prepare-release job that runs before the build matrix.

    Without this, all three platform jobs run in parallel. Mac's electron-builder
    uploads assets to the GitHub release within ~60s of starting. If Windows
    reaches the asset-clearing step after Mac has uploaded, it tries to delete
    Mac's assets mid-upload, causing a race condition. prepare-release serialises
    the clear step before any platform build starts.
    """
    yml = (Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text()
    assert "prepare-release:" in yml, (
        "build.yml is missing a 'prepare-release' job. "
        "Add it to run asset-clearing before the build matrix starts."
    )
    # Accept prepare-release alone or alongside other deps (e.g. setup-matrix).
    assert "needs: [prepare-release" in yml or "needs: prepare-release" in yml, (
        "The build matrix job does not declare 'needs: prepare-release'. "
        "Without this, the build matrix runs in parallel with prepare-release."
    )


def test_main_auto_install_on_quit_disabled():
    """autoInstallOnAppQuit must be false.

    With true, Squirrel installs on any normal quit without admin confirmation.
    This creates an uncontrolled background install that cannot be monitored or
    tested. The only install trigger must be an explicit admin action.
    """
    src = _main_src()
    assert "autoInstallOnAppQuit = false" in src, (
        "main.js sets autoInstallOnAppQuit = true. "
        "This allows Squirrel to install silently on normal quit, creating a "
        "background race condition. Set it to false."
    )
    assert "autoInstallOnAppQuit = true" not in src, (
        "main.js still has autoInstallOnAppQuit = true. Remove it."
    )


def test_main_periodic_check_interval():
    """main.js must schedule a periodic update check (every 4 hours).

    Without a periodic check, users who keep the app open for days will miss
    newly released versions until they manually restart.
    """
    src = _main_src()
    assert "setInterval" in src, (
        "main.js does not schedule a periodic update check via setInterval. "
        "Long-running sessions will never see new releases."
    )
    # 4 hours in ms = 14400000; also accept the expression form
    assert "14400000" in src or "4 * 60 * 60 * 1000" in src, (
        "main.js periodic check interval is not 4 hours (14400000 ms). "
        "Use setInterval(..., 4 * 60 * 60 * 1000)."
    )


def test_shell_no_is_manual_check_gate():
    """shell.py must not gate log output behind isManualCheck.

    All update log lines — including from background checks — must be shown.
    Errors in particular must always be visible regardless of who triggered the check.
    """
    src = _shell_src()
    # Check for the variable declaration/assignment, not just the word in comments
    assert "var isManualCheck" not in src, (
        "shell.py still declares var isManualCheck. Remove it. "
        "All update log lines must be shown unconditionally."
    )
    assert "isManualCheck = true" not in src, (
        "shell.py still sets isManualCheck = true. Remove it."
    )


def test_main_js_mac_hide_on_close():
    """main.js must intercept the 'close' event on darwin and hide instead of destroy.

    Without this, clicking the red X on macOS destroys the BrowserWindow and the
    next dock-icon click shows an endless "Starting..." loader (startup not re-run).
    """
    src = _main_src()
    assert 'process.platform === "darwin"' in src, (
        'main.js does not check process.platform === "darwin" in a close handler. '
        "Mac users will get the endless Starting... bug when reopening from dock."
    )
    # Verify the hide() call exists and event.preventDefault() suppresses destroy
    assert "mainWindow.hide()" in src, (
        "main.js does not call mainWindow.hide(). "
        "Red-X click will destroy the window instead of hiding it on macOS."
    )
    assert "event.preventDefault()" in src, (
        "main.js does not call event.preventDefault() in the close handler. "
        "The BrowserWindow will still be destroyed despite hide() call."
    )


def test_main_js_activate_shows_hidden_window():
    """The 'activate' handler must call mainWindow.show() for the hide-on-close path."""
    src = _main_src()
    act_idx = src.find('app.on("activate"')
    assert act_idx != -1, "activate handler not found in main.js"
    block = src[act_idx: act_idx + 300]
    assert "mainWindow.show()" in block, (
        "activate handler does not call mainWindow.show(). "
        "Clicking the dock icon will not restore the hidden window."
    )


def test_main_js_cmd_q_quits_fully():
    """Cmd+Q (before-quit) must set isQuitting=true so the close handler lets it through.

    Without this, event.preventDefault() in the 'close' handler intercepts the
    quit-triggered close event too, making Cmd+Q appear to do nothing on macOS.
    """
    src = _main_src()
    assert "isQuitting" in src, (
        "main.js does not use an isQuitting flag. "
        "Cmd+Q will be intercepted by the close handler and appear to do nothing on macOS."
    )
    assert "isQuitting = true" in src, (
        "main.js never sets isQuitting = true. "
        "The before-quit handler must set it so the close handler stops hiding the window."
    )
    # The close handler must check the flag before hiding
    close_idx = src.find("mainWindow.on(\"close\"")
    assert close_idx != -1, "close handler not found"
    close_block = src[close_idx: close_idx + 300]
    assert "isQuitting" in close_block, (
        "The close handler does not check isQuitting. "
        "Cmd+Q will still be intercepted and the app will not quit."
    )


# ---------------------------------------------------------------------------
# CI build identity
# ---------------------------------------------------------------------------

def test_build_workflow_stamps_the_electron_version_with_the_tested_script():
    """Every build, tag or not, stamps electron/package.json through one script."""
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text()
    start = workflow.index("- name: Set Electron version from git tag or development commit")
    step = workflow[start:workflow.index("- name:", start + 1)]
    assert "if:" not in step
    assert "run: python3 scripts/electron_version.py" in step


def test_build_workflow_signs_all_non_pr_macos_dev_builds():
    """Every non-PR macOS build must use the Developer ID signing path."""
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text()

    prep_idx = workflow.index("- name: Prepare macOS signing keychain")
    prep_end = workflow.index("\n      - name:", prep_idx + 1)
    prep = workflow[prep_idx:prep_end]
    assert "github.event_name != 'pull_request'" in prep
    assert "github.ref_name != 'bugfix'" not in prep

    dev_idx = workflow.index("- name: Build macOS (development - signed only, no notarization)")
    dev_end = workflow.index("\n      - name:", dev_idx + 1)
    dev = workflow[dev_idx:dev_end]
    assert "!startsWith(github.ref, 'refs/tags/v')" in dev
    assert "github.event_name != 'pull_request'" in dev
    assert "github.ref_name != 'bugfix'" not in dev

    pr_idx = workflow.index("- name: Build macOS (pull request - UNSIGNED)")
    pr_end = workflow.index("\n      - name:", pr_idx + 1)
    pr = workflow[pr_idx:pr_end]
    assert "github.event_name == 'pull_request'" in pr
    assert 'CSC_IDENTITY_AUTO_DISCOVERY: "false"' in pr


def test_build_workflow_validates_final_macos_dmg_before_distribution():
    """The exact DMG users receive must verify, mount, and contain Celerp.app."""
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text()

    verify_idx = workflow.index("- name: Verify macOS distributable")
    verify_end = workflow.index("\n      - name:", verify_idx + 1)
    verify = workflow[verify_idx:verify_end]

    assert "if: matrix.os == 'macos-latest'" in verify
    assert 'hdiutil verify "$DMG"' in verify
    assert 'hdiutil attach -readonly -nobrowse -mountpoint "$MOUNT_POINT" "$DMG"' in verify
    assert 'test -d "$MOUNT_POINT/Celerp.app"' in verify
    assert 'hdiutil detach "$MOUNT_POINT"' in verify
    assert 'codesign --verify --deep --strict --verbose=2 "$MOUNT_POINT/Celerp.app"' in verify
    assert 'xcrun stapler validate "$MOUNT_POINT/Celerp.app"' in verify
    assert 'if [[ "$GITHUB_REF" == refs/tags/v* ]]' in verify

    # Both dev artifact upload and tag publication are downstream of this build step.
    assert verify_idx < workflow.index("- name: Upload artifacts (dev builds only)")
    assert "publish-release:" in workflow
    publish_idx = workflow.index("  publish-release:")
    publish_block = workflow[publish_idx:publish_idx + 300]
    needs_line = next(l for l in publish_block.splitlines() if l.strip().startswith("needs:"))
    assert needs_line.strip() == "needs: [prepare-release, setup-matrix, build, openapi-asset]"


def test_build_workflow_exports_versioned_openapi_before_publish():
    """Tag releases must publish one rerunnable, version-matched OpenAPI asset."""
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text()

    openapi_idx = workflow.index("  openapi-asset:")
    publish_idx = workflow.index("  publish-release:")
    openapi_block = workflow[openapi_idx:publish_idx]

    assert "needs: [build]" in openapi_block
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CELERP=${GITHUB_REF_NAME#v}" in openapi_block
    assert "python scripts/export_openapi.py --out openapi.json" in openapi_block
    assert 'schema["info"]["version"]' in openapi_block
    assert 'expected = os.environ["GITHUB_REF_NAME"].removeprefix("v")' in openapi_block
    assert "EXISTING_ASSET_ID=" in openapi_block
    assert "/releases/assets/$EXISTING_ASSET_ID" in openapi_block
    assert "assets?name=openapi.json" in openapi_block


# ---------------------------------------------------------------------------
# Installer upgrade and downgrade checks
# ---------------------------------------------------------------------------

_WORKFLOWS = Path(__file__).parent.parent / ".github" / "workflows"


def _workflow(name):
    import yaml
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def test_windows_installer_check_covers_every_starting_point():
    """none / same / older / newer / --updated, and a refused run changes nothing."""
    steps = {s.get("name"): s for s in _workflow("build.yml")["jobs"]["build"]["steps"]}
    run = steps["Installer version check (Windows)"]["run"]
    for case in ("none:", "same:", "installed newer:", "--updated:", "installed older:"):
        assert f'Write-Host "{case}' in run, case
    assert 'Get-FileHash (Join-Path $dir "Celerp.exe") -Algorithm SHA256' in run
    assert "celerp-ci-sentinel.txt" in run
    assert 'if ($after -ne $before) { Fail "older installer changed the install' in run
    assert 'if ($code -ne 2) { Fail "older installer over 999.0.0' in run


def test_packaged_build_checks_its_modules_and_boots_with_every_locked_module():
    """Every build platform checks the packaged default modules against the lock
    with the packaged Python, then boots the packaged app with every module in
    the lock enabled."""
    steps = _workflow("build.yml")["jobs"]["build"]["steps"]
    names = [s.get("name") for s in steps]
    by_name = dict(zip(names, steps))
    check = by_name["Check bundled modules in the packaged artifact"]
    assert "if" not in check
    assert ('PYTHONPATH="$RES/app" "$PY" scripts/check_packaged_modules.py \\\n'
            '  "$RES/app/default_modules" --lock default_modules/first_party.lock.json'
            in check["run"])
    for label, py in (("macos-latest)", "python-arm64/python/bin/python3"),
                      ("windows-latest)", "python-x64/python/python.exe"),
                      ("*)", "python-x64/python/bin/python3")):
        assert f'PY="$RES/{py}"' in check["run"].split(label, 1)[1].split(";;", 1)[0]

    unix = by_name["Boot smoke (launch the packaged app, require db:ok)"]
    win = by_name["Boot smoke (Windows, require db:ok)"]
    for smoke in (unix, win):
        assert names.index(check["name"]) < names.index(smoke["name"])
    assert ('json.load(open("../default_modules/first_party.lock.json"))' in unix["run"]
            and "export ENABLED_MODULES" in unix["run"])
    # Linux and macOS: a force-quit app (SIGKILL) must leave no API or database running.
    assert 'kill -9 "$ELECTRON"' in unix["run"] and "left the API or the database running" in unix["run"]
    assert ('Get-Content "..\\default_modules\\first_party.lock.json" -Raw | ConvertFrom-Json'
            in win["run"])
    assert "set ENABLED_MODULES=$enabled" in win["run"]


def test_packaged_upgrade_smoke_runs_nightly_and_on_demand_only():
    wf = _workflow("packaged-upgrade-smoke.yml")
    triggers = wf[True]  # YAML 1.1 reads the bare key `on` as True
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    steps = [s.get("name") for s in wf["jobs"]["upgrade"]["steps"]]
    assert "Previous, candidate, downgrade, reopen (Linux, data)" in steps
    assert "Previous, candidate, downgrade, in-app update run (Windows, install)" in steps
