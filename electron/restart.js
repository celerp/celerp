// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Pure functions extracted from app-main.js for unit testing.
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

/**
 * Classify a navigation/window-open URL for the app frame.
 *
 * The app's own UI lives on http://127.0.0.1:<uiPort>, and a restart re-allocates
 * that port. A URL pointing at a FORMER UI port is a stale self-reference (a redirect
 * or link issued before the restart); its server is gone, so handing it to the OS
 * browser just opens a dead tab. It must be dropped: the restart flow already reloads
 * the window to the live port. Loopback URLs on ports we never served stay "external"
 * (users legitimately link other local tools), as do all other http(s) URLs.
 *
 * Ports are compared exactly (not by string prefix, which would let :3630 match :36303).
 *
 * @param {string} url
 * @param {number|null} uiPort - current UI port
 * @param {Set<number>} [formerUiPorts] - UI ports used earlier in this app session
 * @returns {"allow"|"deny"|"external"} allow in-window / drop / open in OS browser
 */
function classifyNavigation(url, uiPort, formerUiPorts = new Set()) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return "deny";
  }
  if (u.protocol === "http:" && u.hostname === "127.0.0.1") {
    const port = Number(u.port);
    if (uiPort !== null && port === Number(uiPort)) return "allow";
    if (formerUiPorts.has(port)) return "deny";
    return "external";
  }
  if (u.protocol === "https:" || u.protocol === "http:") return "external";
  return "deny";
}

module.exports = { restartSentinelPath, watchForRestart, classifyNavigation };
