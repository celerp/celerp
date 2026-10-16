// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Windows start under an administrator token. The bundled Postgres refuses to run
// as an administrator, so an elevated start relaunches the app through Explorer,
// which (with UAC on) starts it with the user's standard token. With UAC off, or
// as the built-in Administrator, Explorer is elevated too and the relaunched copy
// would relaunch again forever. Explorer passes no arguments or environment to
// the copy, so the attempt is recorded in a marker file on disk: an elevated start
// that finds a fresh marker boots in place and starts Postgres with the
// administrator rights removed instead.
"use strict";

const fs = require("fs");
const path = require("path");

// A relaunched copy starts within seconds; an older marker is from an earlier run.
const MARKER_MAX_AGE_MS = 60 * 1000;

/**
 * What an app start should do: "relaunch" through Explorer, "boot-restricted"
 * (boot here, Postgres without administrator rights) or "boot" as normal.
 * `markerAgeMs` is the age of the relaunch marker, or null when there is none.
 */
function deelevationAction({ elevated, markerAgeMs }) {
  if (!elevated) return "boot";
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
 * Decide this start's action from the elevation state and the marker, writing the
 * marker before a relaunch. Every start consumes the marker, so it never outlives
 * the relaunch it records. A marker that cannot be written means the relaunched
 * copy could not tell it is one, so this start boots restricted rather than risk
 * a loop.
 */
function startupDeelevation({ elevated, markerPath, now }) {
  const action = deelevationAction({ elevated, markerAgeMs: consumeMarker(markerPath, now) });
  if (action !== "relaunch") return action;
  try {
    fs.mkdirSync(path.dirname(markerPath), { recursive: true });
    fs.writeFileSync(markerPath, String(now));
    return "relaunch";
  } catch {
    return "boot-restricted";
  }
}

module.exports = { MARKER_MAX_AGE_MS, deelevationAction, startupDeelevation };
