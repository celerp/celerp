// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Windows start under an administrator token. The bundled Postgres refuses to run
// as an administrator. The token state comes from the app's own access token
// (celerp/win_restricted_launch.py --token-state):
//   "standard"  no administrator rights: boot as normal.
//   "elevated"  UAC on, Run as administrator: relaunch through Explorer, which
//               starts the copy with the user's standard token.
//   "admin"     UAC off, or the built-in Administrator: there is no standard token
//               to go back to, so boot here and start Postgres with the
//               administrator rights removed.
// A token that cannot be read boots restricted too, which works for any account.
// The one relaunch that can come back elevated is an elevated Explorer; Explorer
// passes no arguments or environment to the copy, so the attempt is recorded in a
// marker file on disk and an elevated start that finds a fresh marker boots
// restricted instead of relaunching again.
"use strict";

const fs = require("fs");
const path = require("path");

// A relaunched copy starts within seconds; an older marker is from an earlier run.
const MARKER_MAX_AGE_MS = 60 * 1000;

/**
 * What an app start should do: "relaunch" through Explorer, "boot-restricted"
 * (boot here, Postgres without administrator rights) or "boot" as normal.
 * `token` is the token state, or null when it could not be read; `markerAgeMs`
 * is the age of the relaunch marker, or null when there is none.
 */
function deelevationAction({ token, markerAgeMs }) {
  if (token === "standard") return "boot";
  if (token !== "elevated") return "boot-restricted";
  const fresh = markerAgeMs !== null && markerAgeMs >= 0 && markerAgeMs < MARKER_MAX_AGE_MS;
  return fresh ? "boot-restricted" : "relaunch";
}

/** Read and delete the relaunch marker; returns its age in ms, or null when absent or unreadable. */
function consumeMarker(markerPath, now) {
  let written;
  try {
    written = Number(fs.readFileSync(markerPath, "utf8").trim());
  } catch {
    return null;
  }
  try { fs.rmSync(markerPath, { force: true }); } catch { /* a marker that stays is ignored once stale */ }
  return Number.isFinite(written) ? now - written : null;
}

/**
 * Decide this start's action from the token state and the marker, writing the
 * marker before a relaunch. Every start consumes the marker, so it never outlives
 * the relaunch it records. A marker that cannot be written means the relaunched
 * copy could not tell it is one, so this start boots restricted rather than risk
 * a loop.
 */
function startupDeelevation({ token, markerPath, now }) {
  const action = deelevationAction({ token, markerAgeMs: consumeMarker(markerPath, now) });
  if (action !== "relaunch") return action;
  try {
    fs.mkdirSync(path.dirname(markerPath), { recursive: true });
    fs.writeFileSync(markerPath, String(now));
    return "relaunch";
  } catch {
    return "boot-restricted";
  }
}

// Explorer hands the app on and exits within a second or two.
const EXPLORER_TIMEOUT_MS = 15 * 1000;

/**
 * Hand the app back to Explorer (the shell) to start. This also sidesteps
 * Electron's job object: Electron runs its own children in a job with
 * KILL_ON_JOB_CLOSE, so a direct relaunch would die the moment this instance
 * exits, but an Explorer-launched process is the shell's child and survives.
 * Waits until Explorer has taken it, so the caller quits only once the relaunch
 * is under way. Explorer's exit code means nothing (it is 1 on success), so only
 * a failure to run it counts. Returns true when the relaunch is under way.
 */
function relaunchThroughExplorer({ spawnSync, systemRoot, exePath }) {
  try {
    const explorer = path.join(systemRoot || "C:\\Windows", "explorer.exe");
    const result = spawnSync(explorer, [exePath], { timeout: EXPLORER_TIMEOUT_MS, windowsHide: true });
    if (result.error) {
      console.error("[startup] de-elevation relaunch failed:", result.error);
      return false;
    }
    return true;
  } catch (e) {
    console.error("[startup] de-elevation relaunch failed:", e);
    return false;
  }
}

module.exports = { MARKER_MAX_AGE_MS, deelevationAction, startupDeelevation, relaunchThroughExplorer };
