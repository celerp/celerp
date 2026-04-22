// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BSL-1.1
//
// Pure functions extracted from main.js for unit testing.
// No Electron imports — safe to require in Jest.

"use strict";

const path = require("path");

/**
 * Compute the restart sentinel path, mirroring celerp/config.py:
 *   config_path().parent / ".restart_requested"
 *
 * Mac/Linux: $XDG_CONFIG_HOME/celerp or ~/.config/celerp
 * Windows:   %APPDATA%/celerp
 *
 * @param {typeof process} [proc] - injectable for testing
 * @returns {string}
 */
function restartSentinelPath(proc) {
  const p = proc || process;
  let base;
  if (p.platform === "win32") {
    base = p.env.APPDATA || path.join(p.env.USERPROFILE || "", "AppData", "Roaming");
  } else {
    base = p.env.XDG_CONFIG_HOME || path.join(p.env.HOME || "", ".config");
  }
  return path.join(base, "celerp", ".restart_requested");
}

/**
 * Attach an exit handler to the current apiProcess reference.
 * If the restart sentinel is present on exit: delete it, respawn API + UI,
 * then re-attach the watcher (so subsequent restarts also work).
 * If absent and exit code is non-zero: call onCrash.
 *
 * @param {string} dbUrl
 * @param {{
 *   getApiProcess: () => import("child_process").ChildProcess | null,
 *   getUiProcess: () => import("child_process").ChildProcess | null,
 *   setUiProcess: (p: null) => void,
 *   startApi: (dbUrl: string) => Promise<void>,
 *   startUi: (dbUrl: string) => Promise<void>,
 *   sentinelPath: string,
 *   onCrash: (err: Error) => void,
 *   onRestart?: () => void,
 *   fs?: typeof import("fs"),
 * }} deps
 */
function watchForRestart(dbUrl, {
  getApiProcess,
  getUiProcess,
  setUiProcess,
  startApi,
  startUi,
  sentinelPath,
  onCrash,
  onRestart,
  fs: fsOverride,
}) {
  const proc = getApiProcess();
  if (!proc) return;
  const fs = fsOverride || require("fs");

  proc.once("exit", async (code) => {
    if (fs.existsSync(sentinelPath)) {
      fs.unlinkSync(sentinelPath);
      console.log("[restart] Sentinel found — respawning API and UI...");
      try {
        const ui = getUiProcess();
        if (ui) { ui.kill(); setUiProcess(null); }
        await startApi(dbUrl);
        await startUi(dbUrl);
        watchForRestart(dbUrl, { getApiProcess, getUiProcess, setUiProcess, startApi, startUi, sentinelPath, onCrash, onRestart, fs });
        console.log("[restart] Servers respawned.");
        if (onRestart) onRestart();
      } catch (err) {
        onCrash(err);
      }
    } else if (code !== 0 && code !== null) {
      onCrash(new Error(`API process exited unexpectedly with code ${code}`));
    }
  });
}

module.exports = { restartSentinelPath, watchForRestart };
