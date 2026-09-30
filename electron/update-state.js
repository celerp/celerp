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
  return Object.freeze({ status: "idle", version: "", percent: 0, message: "" });
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
 *
 * @param {{status: string, version: string, percent: number, message: string}} state
 * @param {{type: "found"|"progress"|"downloaded"|"not-available"|"error",
 *          version?: string, percent?: number, message?: string}} event
 */
function nextUpdateState(state, event) {
  if (state.status === "downloaded") return null;
  const next = (fields) => Object.freeze({ ...state, message: "", ...fields });
  switch (event.type) {
    case "found": {
      const version = event.version || "";
      const same = state.status === "downloading" && state.version === version;
      return next({ status: "downloading", version, percent: same ? state.percent : 0 });
    }
    case "progress":
      return next({ status: "downloading", percent: toPercent(event.percent) });
    case "downloaded":
      return next({ status: "downloaded", version: event.version || state.version, percent: 100 });
    case "not-available":
      if (state.status === "downloading") return null;
      return initialUpdateState();
    case "error":
      return next({ status: "error", percent: 0, message: event.message || "" });
    default:
      return null;
  }
}

/**
 * Track an electron-updater instance. The state and its log lines go to the
 * window together, as `send(channel, {...state, log})`, whenever either
 * changes, so the window can never be shown a demoted state. Each check starts
 * a fresh log. An error is always logged, even once downloaded, when it no
 * longer changes the state.
 *
 * @param {{on: (event: string, fn: Function) => void}} updater
 * @param {(channel: string, state: object) => void} send
 * @returns {() => object} getUpdateState, returning a copy with its log
 */
function trackUpdater(updater, send) {
  let state = initialUpdateState();
  let log = [];
  // Progress is logged once per 10% step, so the log stays short.
  let loggedStep = -1;

  const snapshot = () => ({ ...state, log: [...log] });

  // Apply `event`; `lineFor(state)` gives the log line for it, if any.
  // `logIgnored` still logs the line when the state ignores the event.
  function apply(channel, event, lineFor, logIgnored) {
    const next = nextUpdateState(state, event);
    if (!next && !logIgnored) return;
    if (next) state = next;
    const line = lineFor(state);
    if (line) log = log.concat(line).slice(-MAX_LOG_LINES);
    send(channel, snapshot());
  }

  updater.on("checking-for-update", () => {
    log = ["Checking for update..."];
    send("update-log", snapshot());
  });
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
      (s) => (s.version ? "v" + s.version : "The update") + " ready. Click 'Restart to Install'"));
  updater.on("update-not-available", () =>
    apply("update-not-available", { type: "not-available" }, () => ""));
  updater.on("error", (err) => {
    const message = typeof err === "string" ? err : (err && err.message) || "";
    apply("update-error", { type: "error", message },
      () => (message ? "Update error: " + message : "Update error"), true);
  });

  return snapshot;
}

/**
 * Serve the updater state to the window: answer "get-update-state" (idle with
 * an empty log until an updater is tracked, as in dev builds) and forward every
 * change to the current window. Returns `track(updater)`.
 *
 * @param {{handle: (channel: string, fn: Function) => void}} ipcMain
 * @param {() => ({webContents: {send: Function}}|null)} getWindow
 */
function serveUpdateState(ipcMain, getWindow) {
  let getUpdateState = () => ({ ...initialUpdateState(), log: [] });
  ipcMain.handle("get-update-state", () => getUpdateState());
  return function track(updater) {
    getUpdateState = trackUpdater(updater, (channel, state) => {
      const win = getWindow();
      if (win) win.webContents.send(channel, state);
    });
  };
}

module.exports = { initialUpdateState, nextUpdateState, trackUpdater, serveUpdateState };
