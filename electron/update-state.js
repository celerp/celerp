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

const errorLine = (message) => (message ? "Update error: " + message : "Update error");

const RELEASES = "https://github.com/celerp/celerp/releases";

// The release page with the installer for `version`, or the latest release.
function releasePageUrl(version) {
  return version ? `${RELEASES}/tag/v${version}` : `${RELEASES}/latest`;
}

function initialUpdateState() {
  return Object.freeze({ status: "idle", version: "", percent: 0, message: "", checking: false,
                         downloadFailed: false, failure: "", releaseUrl: "" });
}

/**
 * What failed, from the updater's error: "install" (the installer did not
 * start), "save" (Windows refused to save the download, usually antivirus
 * still scanning it), "verify" (the download failed its checksum or signature
 * check), "download", or "check". A download that failed verification must not
 * be installed by hand, so it is the one failure with no installer to offer.
 *
 * @param {string} message the updater's error
 * @param {boolean} downloading whether a download was under way
 */
function failureKind(message, downloading) {
  if (/^(EPERM|EACCES|EBUSY):/.test(message)) return "save";
  if (/sha512|checksum|signature|not signed/i.test(message)) return "verify";
  return downloading ? "download" : "check";
}

// The failed state for `failure`, with the release page to offer, if any.
function failedState(state, failure, fields) {
  return Object.freeze({ ...state, status: "error", percent: 0, checking: false, failure,
                         releaseUrl: failure === "verify" ? "" : releasePageUrl(fields.version ?? state.version),
                         ...fields });
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
 * - "install-failed" (the installer did not start) replaces any state, even
 *   downloaded, with a failed download that offers the installer.
 *
 * @param {{status: string, version: string, percent: number, message: string,
 *          checking: boolean, downloadFailed: boolean, failure: string, releaseUrl: string}} state
 * @param {{type: "check"|"dismiss"|"found"|"progress"|"downloaded"|"not-available"|"error"|"install-failed",
 *          version?: string, percent?: number, message?: string}} event
 */
function nextUpdateState(state, event) {
  if (event.type === "install-failed") {
    return failedState(state, "install", { version: event.version || state.version, message: event.message || "",
                                           downloadFailed: true });
  }
  if (state.status === "downloaded") return null;
  const failedDownload = state.status === "error" && state.downloadFailed;
  const next = (fields) =>
    Object.freeze({ ...state, message: "", checking: false, downloadFailed: false, failure: "", releaseUrl: "", ...fields });
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
    case "error": {
      const message = event.message || "";
      const downloadFailed = state.status === "downloading" || failedDownload;
      return failedState(state, failureKind(message, downloadFailed), { message, downloadFailed });
    }
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
 * when it no longer changes the state. `onFailure(state)` hears of the first
 * failure after a check the user asked for (userChecks); the app's own checks
 * and downloads fail on the update card only.
 *
 * @param {{on: (event: string, fn: Function) => void}} updater
 * @param {(channel: string, state: object) => void} send
 * @param {(state: object) => void} [onFailure]
 * @returns {{getUpdateState: () => object, dismissError: () => void, userChecks: () => void,
 *            installFailed: (version: string, message: string) => void}}
 *   getUpdateState returns a copy with its log and revision; dismissError clears a failed
 *   download, at the user's request; userChecks marks the next check as the user's;
 *   installFailed records an installer that did not start.
 */
function trackUpdater(updater, send, onFailure = () => {}) {
  let state = initialUpdateState();
  let log = [];
  // Progress is logged once per 10% step, so the log stays short.
  let loggedStep = -1;
  // Rises with every change sent, so the window can tell an older state from a newer one.
  let revision = 0;
  // Set while a check the user asked for has no result yet.
  let userAsked = false;

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
  updater.on("update-downloaded", (info) => {
    userAsked = false;
    apply("update-downloaded", { type: "downloaded", version: info && info.version },
      (s) => (s.version ? "v" + s.version : "The update") + " ready. Restart to install.");
  });
  updater.on("update-not-available", () => {
    userAsked = false;
    apply("update-not-available", { type: "not-available" }, () => "");
  });
  updater.on("error", (err) => {
    const message = typeof err === "string" ? err : (err && err.message) || "";
    apply("update-error", { type: "error", message }, () => errorLine(message), true);
    if (userAsked) {
      userAsked = false;
      onFailure(snapshot());
    }
  });

  return {
    getUpdateState: snapshot,
    dismissError: () => apply("update-log", { type: "dismiss" }, () => ""),
    userChecks: () => { userAsked = true; },
    installFailed: (version, message) =>
      apply("update-error", { type: "install-failed", version, message }, () => errorLine(message)),
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
 * @param {(state: object) => void} [onFailure] see trackUpdater
 * @returns {{getUpdateState: () => object, installFailed: Function, checkForUser: () => void}}
 *   installFailed as in trackUpdater; checkForUser runs a check as the user's.
 */
function serveUpdateState(ipcMain, getWindow, updater, canCheck, onFailure) {
  const { getUpdateState, dismissError, userChecks, installFailed } = trackUpdater(updater, (channel, state) => {
    const win = getWindow();
    if (win) win.webContents.send(channel, state);
  }, onFailure);
  ipcMain.handle("get-update-state", () => getUpdateState());
  // A check the user asks for dismisses a failed download; the checks the app
  // runs by itself never do.
  const checkForUser = () => {
    dismissError();
    if (!canCheck) return;
    userChecks();
    updater.checkForUpdates().catch(() => {}); // errors handled by the "error" event
  };
  ipcMain.handle("check-for-updates", checkForUser);
  return { getUpdateState, installFailed, checkForUser };
}

module.exports = { initialUpdateState, nextUpdateState, trackUpdater, serveUpdateState };
