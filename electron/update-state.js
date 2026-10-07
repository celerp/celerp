// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Updater state kept in the main process, so a page that loads after an
// updater event can still show it. The renderer asks for this state when a
// page loads or is restored from history, and renders it with the same code
// it uses for live events.
//
// No Electron imports: safe to require in Jest, and the state lives only in
// memory, so a relaunch starts idle.

"use strict";

// Log lines kept for the update card, oldest dropped first.
const MAX_LOG_LINES = 200;

function initialUpdateState() {
  return Object.freeze({ status: "idle", version: "", percent: 0, message: "", checking: false, downloadFailed: false });
}

// A download percent from 0 to 100; anything that is not a number is 0.
function toPercent(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.min(100, Math.max(0, n)) : 0;
}

/**
 * Apply one updater event. Returns the new state, or null when the event is
 * ignored.
 *
 * - "downloaded" is terminal for the running app: nothing moves it.
 * - not-available while downloading is ignored: the download in progress still
 *   ends in downloaded or error.
 * - a check marks an idle or failed state as checking, keeping the rest of it,
 *   until the check has a result. A failed check is replaced only by that
 *   result: not-available (up to date), found, or another error.
 * - a failed download (downloadFailed) stays until a retried download finishes
 *   or the user dismisses it: checks, found and progress leave it as it is.
 *
 * @param {{status: string, version: string, percent: number, message: string,
 *          checking: boolean, downloadFailed: boolean}} state
 * @param {{type: "check"|"dismiss"|"found"|"progress"|"downloaded"|"not-available"|"error",
 *          version?: string, percent?: number, message?: string}} event
 */
function nextUpdateState(state, event) {
  if (state.status === "downloaded") return null;
  const failedDownload = state.status === "error" && state.downloadFailed;
  const next = (fields) =>
    Object.freeze({ ...state, message: "", checking: false, downloadFailed: false, ...fields });
  switch (event.type) {
    case "check":
      if ((state.status !== "idle" && state.status !== "error") || failedDownload) return null;
      return Object.freeze({ ...state, checking: true });
    case "dismiss":
      if (!failedDownload) return null;
      return Object.freeze({ ...state, downloadFailed: false });
    case "found": {
      if (failedDownload) return null;
      const version = event.version || "";
      const same = state.status === "downloading" && state.version === version;
      return next({ status: "downloading", version, percent: same ? state.percent : 0 });
    }
    case "progress":
      if (failedDownload) return null;
      return next({ status: "downloading", percent: toPercent(event.percent) });
    case "downloaded":
      return next({ status: "downloaded", version: event.version || state.version, percent: 100 });
    case "not-available":
      if (state.status === "downloading" || failedDownload) return null;
      return initialUpdateState();
    case "error":
      return next({ status: "error", percent: 0, message: event.message || "",
                    downloadFailed: state.status === "downloading" || failedDownload });
    default:
      return null;
  }
}

/**
 * Track an electron-updater instance. The state and its log lines go to the
 * window together, as `send(channel, {...state, log, revision})`, whenever
 * either changes, so the window can never be shown a demoted state. `revision`
 * rises with each change, so a state read earlier but delivered later (the
 * page's get-update-state answer racing a live event) is recognisably older. The log is the
 * history of every check, kept until relaunch. An error is always logged, even
 * when it no longer changes the state.
 *
 * @param {{on: (event: string, fn: Function) => void}} updater
 * @param {(channel: string, state: object) => void} send
 * @returns {{getUpdateState: () => object, dismissError: () => void}}
 *   getUpdateState returns a copy with its log and revision; dismissError clears a failed
 *   download, at the user's request.
 */
function trackUpdater(updater, send) {
  let state = initialUpdateState();
  let log = [];
  // Progress is logged once per 10% step, so the log stays short.
  let loggedStep = -1;
  // Rises with every change sent, so the window can tell an older state from a newer one.
  let revision = 0;

  const snapshot = () => ({ ...state, log: [...log], revision });

  // Apply `event`; `lineFor(state)` gives the log line for it, if any.
  // `logIgnored` still logs the line when the state ignores the event.
  function apply(channel, event, lineFor, logIgnored) {
    const next = nextUpdateState(state, event);
    if (!next && !logIgnored) return;
    if (next) state = next;
    const line = lineFor(state);
    if (line) log = log.concat(line).slice(-MAX_LOG_LINES);
    revision += 1;
    send(channel, snapshot());
  }

  // Once downloaded the state is final, so a later check changes nothing, not
  // even the log that says the update is ready.
  updater.on("checking-for-update", () =>
    apply("update-log", { type: "check" }, () => "Checking for update..."));
  updater.on("update-available", (info) => {
    loggedStep = -1;
    apply("update-available", { type: "found", version: info && info.version },
      (s) => (s.version ? "Found v" + s.version : "Found an update") + ", downloading...");
  });
  updater.on("download-progress", (progress) =>
    apply("download-progress", { type: "progress", percent: progress && progress.percent }, (s) => {
      const step = Math.floor(s.percent / 10);
      if (step <= loggedStep) return "";
      loggedStep = step;
      const kbps = Math.round(((progress && Number(progress.bytesPerSecond)) || 0) / 1024);
      return "Downloading: " + Math.round(s.percent) + "% (" + kbps + " KB/s)";
    }));
  updater.on("update-downloaded", (info) =>
    apply("update-downloaded", { type: "downloaded", version: info && info.version },
      (s) => (s.version ? "v" + s.version : "The update") + " ready. Restart to install."));
  updater.on("update-not-available", () =>
    apply("update-not-available", { type: "not-available" }, () => ""));
  updater.on("error", (err) => {
    const message = typeof err === "string" ? err : (err && err.message) || "";
    apply("update-error", { type: "error", message },
      () => (message ? "Update error: " + message : "Update error"), true);
  });

  return {
    getUpdateState: snapshot,
    dismissError: () => apply("update-log", { type: "dismiss" }, () => ""),
  };
}

/**
 * Serve the updater's state to the window: track `updater`, forward every
 * change to the current window, answer "get-update-state", and run the checks
 * the user asks for ("check-for-updates"). The state stays idle with an empty
 * log until the updater checks, which dev builds (`canCheck` false) never do.
 *
 * @param {{handle: (channel: string, fn: Function) => void}} ipcMain
 * @param {() => ({webContents: {send: Function}}|null)} getWindow
 * @param {{on: (event: string, fn: Function) => void, checkForUpdates: () => Promise}} updater
 * @param {boolean} canCheck
 */
function serveUpdateState(ipcMain, getWindow, updater, canCheck) {
  const { getUpdateState, dismissError } = trackUpdater(updater, (channel, state) => {
    const win = getWindow();
    if (win) win.webContents.send(channel, state);
  });
  ipcMain.handle("get-update-state", () => getUpdateState());
  // A check the user asks for dismisses a failed download; the checks the app
  // runs by itself never do.
  ipcMain.handle("check-for-updates", () => {
    dismissError();
    if (canCheck) updater.checkForUpdates().catch(() => {}); // errors handled by the "error" event
  });
}

module.exports = { initialUpdateState, nextUpdateState, trackUpdater, serveUpdateState };
