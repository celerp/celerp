// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Updater state kept in the main process, so a page that loads after an
// updater event can still show it. Every navigation in the app window is a
// full page load, so the renderer asks for this state on load and renders it
// with the same code it uses for live events.
//
// No Electron imports: safe to require in Jest, and the state lives only in
// memory, so a relaunch starts idle.

"use strict";

function initialUpdateState() {
  return Object.freeze({ status: "idle", version: "", percent: 0, message: "" });
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
      return next({ status: "downloading", percent: event.percent || 0 });
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
 * Track an electron-updater instance. Each accepted event updates the state
 * and is forwarded to the window as `send(channel, state)`; ignored events are
 * not forwarded, so the window can never be shown a demoted state either.
 *
 * @param {{on: (event: string, fn: Function) => void}} updater
 * @param {(channel: string, state: object) => void} send
 * @returns {() => object} getUpdateState, returning a copy
 */
function trackUpdater(updater, send) {
  let state = initialUpdateState();

  function apply(channel, event) {
    const next = nextUpdateState(state, event);
    if (!next) return;
    state = next;
    send(channel, { ...state });
  }

  updater.on("update-available", (info) =>
    apply("update-available", { type: "found", version: info && info.version }));
  updater.on("download-progress", (progress) =>
    apply("download-progress", { type: "progress", percent: progress && progress.percent }));
  updater.on("update-downloaded", (info) =>
    apply("update-downloaded", { type: "downloaded", version: info && info.version }));
  updater.on("update-not-available", () =>
    apply("update-not-available", { type: "not-available" }));
  updater.on("error", (err) =>
    apply("update-error", { type: "error", message: typeof err === "string" ? err : (err && err.message) || "" }));

  return () => ({ ...state });
}

module.exports = { initialUpdateState, nextUpdateState, trackUpdater };
