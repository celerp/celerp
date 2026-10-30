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
# app-main.js source
# ---------------------------------------------------------------------------

def _main_src() -> str:
    return _MAIN.read_text()


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

    # Both the artifact upload and tag publication are downstream of this build step.
    assert verify_idx < workflow.index("- name: Upload artifacts")
    assert "publish-release:" in workflow
    publish_idx = workflow.index("  publish-release:")
    publish_block = workflow[publish_idx:publish_idx + 300]
    needs_line = next(l for l in publish_block.splitlines() if l.strip().startswith("needs:"))
    assert needs_line.strip() == "needs: [prepare-release, setup-matrix, build, account-names, openapi-asset, upgrade-smoke]"


def test_build_workflow_exports_versioned_assets_before_publish():
    """Tag releases must publish one rerunnable, version-matched OpenAPI schema and
    importer list."""
    workflow = (Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text()

    openapi_idx = workflow.index("  openapi-asset:")
    publish_idx = workflow.index("  publish-release:")
    openapi_block = workflow[openapi_idx:publish_idx]

    assert "needs: [build]" in openapi_block
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_CELERP=${GITHUB_REF_NAME#v}" in openapi_block
    assert "python scripts/export_openapi.py --out openapi.json" in openapi_block
    assert "python scripts/export_importers.py --out importers.json" in openapi_block
    assert 'schema["info"]["version"]' in openapi_block
    assert 'importers["version"]' in openapi_block
    assert 'expected = os.environ["GITHUB_REF_NAME"].removeprefix("v")' in openapi_block
    assert "for ASSET in openapi.json importers.json; do" in openapi_block
    assert "EXISTING_ASSET_ID=" in openapi_block
    assert "/releases/assets/$EXISTING_ASSET_ID" in openapi_block
    assert "assets?name=$ASSET" in openapi_block


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
    # The full-disk and stopped-install checks look for an entry before anything has
    # installed for the runner's account, when its uninstall key does not exist yet.
    entry = run.split("function Entry {", 1)[1].split("\n          }\n", 1)[0]
    assert entry.index("if (-not (Test-Path $key)) { return }") < entry.index("Get-ChildItem $key")


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
    for smoke, minutes in ((unix, 15), (win, 20)):
        assert names.index(check["name"]) < names.index(smoke["name"])
        # A hung app fails the step with its log instead of holding the runner for hours.
        assert smoke["timeout-minutes"] == minutes
    assert ('json.load(open("../default_modules/first_party.lock.json"))' in unix["run"]
            and "export ENABLED_MODULES" in unix["run"])
    # A boot that never gets ready shows the app's output and logs, then ends the app,
    # which may not act on SIGTERM; the readiness poll itself cannot block.
    assert 'curl -fs --max-time 5 "http://127.0.0.1:$API_PORT/health/ready"' in unix["run"]
    failed = unix["run"].split('if [ -z "$ok" ]; then', 1)[1].split("\nfi\n", 1)[0]
    assert failed.index("-path '*celerp-data/logs/*'") < failed.index('kill -9 "$APP_PID"')
    assert "wait" not in failed
    # The force-quit check reads the database's data directory from the running
    # database; the app does not keep its data under $HOME on every runner.
    force_quit = unix["run"].split("# A force-quit app must not leave its servers running", 1)[1].split("force-quit OK on", 1)[0]
    assert 'PG_PIDFILE="$PG_DATA/postmaster.pid"' in force_quit and 'find "$HOME"' not in force_quit
    # Linux and macOS: a force-quit app (SIGKILL) must leave no API or database running.
    assert 'kill -9 "$ELECTRON"' in unix["run"] and "left the API or the database running" in unix["run"]
    # A fresh install under an account named with spaces and non-ASCII letters: the
    # profile, data and install folders all carry them, and the database must set up.
    assert win["run"] == './scripts/win-boot-smoke.ps1 -Account standard -UserName "Célerp Tëst ü" -OverRunning' and win["working-directory"] == "electron"
    smoke = _win_boot_smoke()
    assert 'Get-Content "..\\default_modules\\first_party.lock.json" -Raw | ConvertFrom-Json' in smoke
    assert "set ENABLED_MODULES=$enabled" in smoke
    # Windows: the job object ends the API and the database when the app is force-quit.
    assert "taskkill /IM Celerp.exe /F" in smoke and "force-quit left the API or the database running" in smoke


def test_packaged_upgrade_smoke_runs_in_the_tag_release_and_on_demand_only():
    """A tag's release build calls the check on its own binaries; a manual run names
    its build. Both take the same candidate checks before either platform."""
    build = _workflow("build.yml")
    assert "schedule" not in build[True]  # YAML 1.1 reads the bare key `on` as True
    assert build[True]["workflow_dispatch"]["inputs"]["platforms"]["options"] == ["all", "windows", "linux", "mac"]
    wf = _workflow("packaged-upgrade-smoke.yml")
    triggers = wf[True]
    assert set(triggers) == {"workflow_call", "workflow_dispatch"}
    for trigger in ("workflow_call", "workflow_dispatch"):
        assert triggers[trigger]["inputs"]["candidate_run"]["required"] is True
        assert triggers[trigger]["inputs"]["previous"]["default"] == ""
    candidate = wf["jobs"]["candidate"]
    assert "if" not in candidate
    check = candidate["steps"][-1]
    assert check["run"] == 'python3 scripts/upgrade_candidate.py check "$CANDIDATE_RUN"'
    assert check["env"]["CANDIDATE_RUN"] == "${{ inputs.candidate_run }}"
    upgrade = wf["jobs"]["upgrade"]
    assert upgrade["needs"] == "candidate"
    # scripts/release_gate.py finds these jobs by name before PyPI publishes.
    assert upgrade["name"] == "upgrade (${{ matrix.os }})"
    assert upgrade["env"]["CANDIDATE_RUN"] == "${{ needs.candidate.outputs.run }}"
    runs = "\n".join(s.get("run", "") for s in upgrade["steps"])
    assert "gh run list" not in runs and "dev-latest" not in runs
    assert 'python3 scripts/upgrade_candidate.py newer "$(version candidate)" "$PREVIOUS_VERSION"' in runs
    assert "python scripts/upgrade_candidate.py newer $new $env:PREVIOUS_VERSION" in runs
    steps = [s.get("name") for s in upgrade["steps"]]
    assert "Previous, candidate, downgrade, reopen (Linux, data)" in steps
    assert "Previous, candidate, downgrade, in-app update run (Windows, install)" in steps


def test_windows_build_ships_the_visual_cpp_runtime_and_checks_every_dll_import():
    """The Windows build stages the Visual C++ runtime for afterPack before packaging,
    then fails when a bundled binary imports a DLL the package does not ship."""
    steps = _workflow("build.yml")["jobs"]["build"]["steps"]
    names = [s.get("name") for s in steps]
    by_name = dict(zip(names, steps))
    stage = by_name["Stage the Visual C++ runtime (Windows)"]
    assert stage["if"] == "runner.os == 'Windows'"
    assert '"${env:ProgramFiles(x86)}\\Microsoft Visual Studio\\Installer\\vswhere.exe" -latest -property installationPath' in stage["run"]
    assert "\\x64\\Microsoft.VC143.CRT" in stage["run"]
    assert 'foreach ($dll in "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll")' in stage["run"]
    assert '"CELERP_VC_RUNTIME_DIR=$dest" | Out-File -Append -Encoding utf8 $env:GITHUB_ENV' in stage["run"]
    for build in ("Build (Windows / Linux - release tag)", "Build (Windows / Linux - dev build)"):
        assert names.index(stage["name"]) < names.index(build)

    closure = by_name["Check the Windows DLL closure"]
    assert closure["if"] == "runner.os == 'Windows'"
    assert "node electron/scripts/check-win-dll-closure.js" in closure["run"]
    assert '"$RES/app.asar.unpacked/node_modules/@embedded-postgres/windows-x64/native/bin"' in closure["run"]
    assert '"$RES/pg-win/bin"' in closure["run"]
    assert names.index("Build (Windows / Linux - dev build)") < names.index(closure["name"])


def _win_boot_smoke():
    return (Path(__file__).parent.parent / "electron" / "scripts" / "win-boot-smoke.ps1").read_text()


def test_windows_install_over_the_running_app_requires_the_app_running_first():
    """-OverRunning proves the installer closes an open app only if the app is open: its
    window, API and database processes and a db:ok API are required before the install,
    and afterwards exactly those processes (by id and start time) must be gone, the
    install whole, and the app must boot again."""
    smoke = _win_boot_smoke()
    over = smoke.split("if ($OverRunning) {", 1)[1].split("\n}\n", 1)[0]
    running_app = smoke.split("function RunningApp(", 1)[1].split("\n}\n", 1)[0]
    for name in ('"Celerp"', '"python"', '"postgres"'):
        assert name in running_app
    assert "/health/ready" in running_app and "Fail " in running_app
    # The processes are kept as values read while the app runs: a process object looks up its path and
    # start time by id when first asked, after the install that may be another process with a reused id.
    assert running_app.rstrip().endswith("$procs | Select-Object Id, Name, Path, StartTime")
    assert over.index("$before = RunningApp $installDir $port") < over.index("InstallSetup $setup")
    assert "Get-Process -Id $_.Id" in over and "$p.StartTime -eq $_.StartTime" in over
    assert over.index("InstallSetup $setup") < over.index("TreeMatches $installDir") < over.index("Launch $exe")


def test_windows_boot_smoke_runs_as_an_administrator_with_uac_off():
    """A second Windows boot smoke installs and launches the app as the built-in
    Administrator, whose full token has no standard token linked to it (what UAC off
    gives an administrator), and requires db:ok. The runner's own account has UAC on,
    so it cannot stand in."""
    steps = _workflow("build.yml")["jobs"]["build"]["steps"]
    names = [s.get("name") for s in steps]
    by_name = dict(zip(names, steps))
    admin = by_name["Boot smoke (Windows administrator, UAC off)"]
    assert admin["if"] == "runner.os == 'Windows'" and admin["timeout-minutes"] == 15
    assert admin["run"] == "./scripts/win-boot-smoke.ps1 -Account admin" and admin["working-directory"] == "electron"
    assert names.index("Boot smoke (Windows, require db:ok)") < names.index(admin["name"])
    smoke = _win_boot_smoke()
    assert "EnableLUA" not in smoke
    admin_setup = smoke.split('if ($Account -eq "standard") {', 1)[1].split("$cred = ", 1)[0].split("} else {", 1)[1]
    assert '$_.SID.Value -like "S-1-5-21-*-500"' in admin_setup and "Enable-LocalUser" in admin_setup
    assert "-Name FilterAdministratorToken -Value 0" in admin_setup
    # The app must not depend on the Server service to read its token.
    assert "Stop-Service LanmanServer -Force" in admin_setup and "net session" not in smoke
    # Both sides of the restricted launcher, read from the installed app's own token
    # check run as that account: "admin", and the launcher's child "standard".
    assert 'if ($state -ne $Account)' in smoke and '$child -ne "standard"' in smoke
    assert smoke.index("--token-state") < smoke.index('$port = Launch $exe "first launch"')
    assert "/health/ready" in smoke and '"db"\\s*:\\s*"ok"' in smoke
    # A launch that never gets ready, and a new install that cannot be set up, show the
    # app's output and every log in the account's data folder (the one before a restart too).
    output = smoke.split("function AppOutput(", 1)[1].split("\n}\n", 1)[0]
    assert "Electron stdout/stderr" in output and "Get-Content $boot -Tail 200" in output
    assert "\\.log(\\.prev)?$" in output
    launch = smoke.split("function Launch(", 1)[1].split("\n}\n", 1)[0]
    assert launch.index("AppOutput $boot") < launch.index('Fail "$what`: db:ok not reached')
    new_install = smoke.split('$port = Launch $exe "new install" -NewInstall', 1)[1].split("ForceQuit", 1)[0]
    assert new_install.index("AppOutput") < new_install.index('Fail "setting up a new install')


def test_checks_that_run_the_packaged_python_leave_the_build_unchanged():
    """The Windows smokes compare each install file by file with win-unpacked, so a
    check that runs the packaged Python must not write bytecode into it."""
    steps = _workflow("build.yml")["jobs"]["build"]["steps"]
    names = [s.get("name") for s in steps]
    built = names.index("Build (Windows / Linux - dev build)")
    first_smoke = min(i for i, n in enumerate(names) if n and n.startswith("Boot smoke"))
    runs_packaged_python = [s for s in steps[built:first_smoke] if '"$PY"' in s.get("run", "")]
    assert {s["name"] for s in runs_packaged_python} == {
        "Check the Windows bundle", "Check bundled modules in the packaged artifact"}
    for step in runs_packaged_python:
        assert step.get("env", {}).get("PYTHONDONTWRITEBYTECODE") == "1", step["name"]


def test_windows_smoke_commands_run_with_the_account_user_name():
    """Start-Process -Credential hands the child the runner's environment, so without
    this the installer sees the runner's USERNAME. The installer only closes a running
    app found with tasklist /FI "USERNAME eq %USERNAME%", so it would miss the
    account's running app and copy over files that app holds open."""
    smoke = _win_boot_smoke()
    env_set = smoke.split("$envSet = ", 1)[1].split("\n", 1)[0]
    assert "set USERNAME=$u`r`n" in env_set
    assert "set USERDOMAIN=$env:COMPUTERNAME`r`n" in env_set


def test_windows_account_names_match_the_named_name_tests():
    """The release build runs the packaged app under every name in ACCOUNT_NAMES, so
    the file-name tests and the Windows accounts cover the same scripts."""
    import yaml

    from test_helpers import ACCOUNT_NAMES

    workflow = yaml.safe_load((Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text())
    rows = workflow["jobs"]["win-names"]["strategy"]["matrix"]["include"]
    assert {r["case"]: r["user"] for r in rows} == ACCOUNT_NAMES


def test_windows_account_rows_expect_2_5_6_to_fail_under_every_non_ascii_name():
    """2.5.6 hands the profile path to the Postgres bootstrap in the ANSI code page, so
    its database setup fails under any name outside ASCII, accents the code page holds
    included ("José" arrives as the byte 0xE9). A row that expects 2.5.6 to start under
    such a name fails at the old release instead of testing the upgrade."""
    import yaml

    workflow = yaml.safe_load((Path(__file__).parent.parent / ".github" / "workflows" / "build.yml").read_text())
    rows = workflow["jobs"]["win-names"]["strategy"]["matrix"]["include"]
    assert {r["case"]: r["fails"] for r in rows} == {r["case"]: not r["user"].isascii() for r in rows}


def test_jobs_downstream_of_the_tag_only_job_run_outside_tags_when_they_should():
    """prepare-release runs only for a tag, so outside a tag it is skipped, and success()
    is false in every job that depends on it, however indirectly. A job gated on
    success() below it is then skipped too: on a pull request the account-name matrix
    never ran and account-names failed with nothing tested. Below prepare-release,
    success() is only for jobs that run for tags alone; the rest check the result of
    the job they need."""
    jobs = _workflow("build.yml")["jobs"]

    def needs(name):
        n = jobs[name].get("needs", [])
        return [n] if isinstance(n, str) else n

    def below_tag_only(name):
        return any(n == "prepare-release" or below_tag_only(n) for n in needs(name))

    for name, job in jobs.items():
        cond = str(job.get("if", ""))
        if below_tag_only(name) and "success()" in cond:
            assert "startsWith(github.ref, 'refs/tags/v')" in cond, name
    assert jobs["win-names"]["if"].startswith("always() && needs.build.result == 'success'")
