// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Pure functions extracted from app-main.js for unit testing.
// No Electron imports, so it is safe to require in Jest.

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
 * then re-attach the watcher (so subsequent restarts also work). When startApi
 * resolves false (the database was refused), nothing else starts.
 * If absent and exit code is non-zero: call onCrash.
 * onWatching runs each time a live API's exit is watched (at boot and after every
 * restart): from then on a restart request is carried out, so that is when the app
 * may tell others where its API is. onStopped runs as soon as a watched API exits,
 * whatever comes next, so the app stops telling others about it.
 * Once the app is stopping, an exit is neither a restart nor a crash, and a
 * restart under way stops before its next step; trackRestart lets the shutdown
 * wait for it.
 *
 * @param {string} dbUrl
 * @param {{
 *   getApiProcess: () => import("child_process").ChildProcess | null,
 *   getUiProcess: () => import("child_process").ChildProcess | null,
 *   setUiProcess: (p: null) => void,
 *   startApi: (dbUrl: string) => Promise<boolean | void>,
 *   startUi: (dbUrl: string) => Promise<void>,
 *   sentinelPath: string,
 *   onCrash: (err: Error) => void,
 *   onRestart?: () => void,
 *   onWatching?: () => void,
 *   onStopped?: () => void,
 *   isStopping: () => boolean,
 *   trackRestart: (restart: Promise<void>) => void,
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
  onWatching,
  onStopped,
  isStopping,
  trackRestart,
  fs: fsOverride,
}) {
  const proc = getApiProcess();
  if (!proc) return;
  const fs = fsOverride || require("fs");
  const deps = { getApiProcess, getUiProcess, setUiProcess, startApi, startUi, sentinelPath, onCrash, onRestart,
                 onWatching, onStopped, isStopping, trackRestart, fs };

  async function respawn() {
    try {
      const ui = getUiProcess();
      if (ui) { ui.kill(); setUiProcess(null); }
      if ((await startApi(dbUrl)) === false || isStopping()) return;
      await startUi(dbUrl);
      if (isStopping()) return;
      watchForRestart(dbUrl, deps);
      console.log("[restart] Servers respawned.");
      if (onRestart) onRestart();
    } catch (err) {
      if (!isStopping()) onCrash(err);
    }
  }

  proc.once("exit", (code) => {
    if (onStopped) onStopped();
    if (isStopping()) return;
    if (fs.existsSync(sentinelPath)) {
      fs.unlinkSync(sentinelPath);
      console.log("[restart] Sentinel found, respawning API and UI...");
      trackRestart(respawn());
    } else if (code !== 0 && code !== null) {
      onCrash(new Error(`API process exited unexpectedly with code ${code}`));
    }
  });
  if (onWatching) onWatching();
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

/**
 * Where the window goes once a restart has moved the UI to a new port: the same
 * page it was showing (path, query and hash), so a page the restart interrupted,
 * such as an import in progress, carries on. A URL that is not the app's own page
 * (the loading screen, a blank window, another site) falls back to the root.
 *
 * @param {string} currentUrl - the window's URL before the reload
 * @param {number} uiPort - the new UI port
 * @param {Set<number>} [formerUiPorts] - UI ports used earlier in this app session
 * @returns {string}
 */
function restartUrl(currentUrl, uiPort, formerUiPorts = new Set()) {
  const root = `http://127.0.0.1:${uiPort}`;
  let u;
  try {
    u = new URL(currentUrl);
  } catch {
    return root;
  }
  const port = Number(u.port);
  const ownPage = u.protocol === "http:" && u.hostname === "127.0.0.1"
    && (port === Number(uiPort) || formerUiPorts.has(port));
  return ownPage ? `${root}${u.pathname}${u.search}${u.hash}` : root;
}

/**
 * Full application relaunch: queue a fresh instance, then quit the current one.
 * This is the only apply path for a DB-mode/url change, because a cold start
 * re-runs resolveDatabaseConfig(readConfig()) and opens the newly-saved database;
 * the sentinel-recycle path reuses the boot-bound dbUrl and would not apply it.
 * Kept as a pure exported primitive (app injected) so it is unit-testable without
 * an Electron runtime.
 *
 * @param {{ relaunch: () => void, quit: () => void }} appRef
 */
function fullRelaunch(appRef) {
  appRef.relaunch();
  appRef.quit();
}

module.exports = { restartSentinelPath, watchForRestart, classifyNavigation, restartUrl, fullRelaunch };
