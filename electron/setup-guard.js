// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Whether Celerp's Windows setup is running, so a Celerp the user starts by hand
// during an update says it is being updated and quits instead of starting its
// servers in a folder setup is about to replace.
//
// Setup holds a named mutex for its whole life (build/installer.nsh, which owns the
// name), and Windows frees it when setup ends or is stopped. Setup also marks its
// temporary folder with a celerp-setup.pid file holding its process ID; that file is
// only a cheap hint that saves the probe on a normal start. A setup that was killed
// leaves its file behind until the next setup removes it, so a hint counts only
// while its process runs.
//
// No Electron imports: safe to require in Jest.

"use strict";

const nodeFs = require("fs");
const path = require("path");

const SETUP_MUTEX = "CelerpSetupRunning";
// Setup starts Celerp with these: --updated when an update is done or stopped, and
// INSTALL_FAILED_ARG with the version when it stopped. The app adds INSTALL_FAILED_ARG
// itself when an installer did not start.
const UPDATED_ARG = "--updated";
const INSTALL_FAILED_ARG = "--update-install-failed=";
const PROBE_TIMEOUT_MS = 3000;

// Exit 0: the mutex exists (opened, or held by an elevated setup this user cannot
// open). Exit 1: it does not. Exit 2: the probe itself failed.
const PROBE = `try { [Threading.Mutex]::OpenExisting('${SETUP_MUTEX}').Dispose(); exit 0 } `
  + "catch [Threading.WaitHandleCannotBeOpenedException] { exit 1 } "
  + "catch [UnauthorizedAccessException] { exit 0 } catch { exit 2 }";

// Signal 0 only asks whether the process exists; EPERM means it does.
function processAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code !== "ESRCH";
  }
}

// A file that names no process (cut short as setup wrote it) still counts: the probe decides.
function setupHint(fs, tempDir, alive) {
  try {
    return fs.readdirSync(tempDir).some((name) => {
      if (!/^ns.*\.tmp$/i.test(name)) return false;
      const file = path.join(tempDir, name, "celerp-setup.pid");
      if (!fs.existsSync(file)) return false;
      const text = fs.readFileSync(file, "utf8").trim();
      return !/^\d+$/.test(text) || alive(Number(text));
    });
  } catch {
    return false;
  }
}

// True only for a start by hand while setup runs. Setup's own starts (its relaunch
// with --updated, its reopen with the failed-install argument) always go ahead, and
// a probe that cannot answer lets Celerp start: setup closes a Celerp it finds
// before it replaces the files.
function setupRunning({ platform, argv, tempDir, spawnSync, log, fs = nodeFs, alive = processAlive }) {
  if (platform !== "win32") return false;
  if (argv.some((arg) => arg === UPDATED_ARG || arg.startsWith(INSTALL_FAILED_ARG))) return false;
  if (!setupHint(fs, tempDir, alive)) return false;
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", PROBE],
    { timeout: PROBE_TIMEOUT_MS, windowsHide: true });
  if (result.status === 0) return true;
  if (result.status === 1) return false;
  log(`[startup] setup guard could not tell whether setup runs, starting: ${result.error?.message ?? `exit ${result.status}`}`);
  return false;
}

module.exports = { setupRunning, SETUP_MUTEX, UPDATED_ARG, INSTALL_FAILED_ARG };
