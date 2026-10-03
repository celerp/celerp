// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// Which Celerp last opened this data directory, and whether this copy may open it.
//
// The record is the marker seedDefaultModules writes into the modules folder of the
// data directory on every version change: the app version that last ran here. An
// older copy started on that data (an old installer run again, an old download) must
// not touch it - its migrations and module seeding would rewind it - so startup asks
// this module first and stops with a plain message instead.
//
// Pure apart from the marker read/write and the injected dialog/shell, so it is
// unit-testable without Electron, like restart.js.

const fs = require("fs");
const path = require("path");

const MARKER_NAME = ".default-modules-version";
const DOWNLOAD_URL = "https://celerp.com/download";

const VERSION_RE = /^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$/;

/** Parse a semantic version; null when the text is not one. Build metadata is ignored. */
function parseVersion(text) {
  const m = VERSION_RE.exec(String(text).trim());
  if (!m) return null;
  return { core: [Number(m[1]), Number(m[2]), Number(m[3])], pre: m[4] ? m[4].split(".") : [] };
}

function _comparePre(a, b) {
  // A release is newer than any prerelease of the same core (2.6.1 > 2.6.1-dev.5).
  if (!a.length || !b.length) return Math.sign(b.length - a.length);
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    const an = /^\d+$/.test(a[i]), bn = /^\d+$/.test(b[i]);
    if (an && bn) {
      const d = Number(a[i]) - Number(b[i]);
      if (d) return Math.sign(d);
    } else if (an !== bn) {
      return an ? -1 : 1;
    } else if (a[i] !== b[i]) {
      return a[i] < b[i] ? -1 : 1;
    }
  }
  return Math.sign(a.length - b.length);
}

/** -1, 0 or 1 as version a is older than, the same as, or newer than version b. */
function compareVersions(a, b) {
  const pa = parseVersion(a), pb = parseVersion(b);
  if (!pa || !pb) throw new Error(`Not a version number: ${pa ? b : a}`);
  for (let i = 0; i < 3; i++) {
    if (pa.core[i] !== pb.core[i]) return Math.sign(pa.core[i] - pb.core[i]);
  }
  return _comparePre(pa.pre, pb.pre);
}

/**
 * Decide from the marker's text (null when there is no marker) whether this copy,
 * running `runningVersion`, may open the data.
 *   { action: "proceed" }                               same or older data, or no record
 *   { action: "refuse", reason: "newer", dataVersion }  a newer Celerp opened it last
 *   { action: "refuse", reason: "unreadable" }          the record cannot be read
 */
function dataVersionDecision(markerText, runningVersion) {
  if (!parseVersion(runningVersion)) {
    throw new Error(`Celerp's own version "${runningVersion}" is not a version number.`);
  }
  if (markerText === null) return { action: "proceed" };
  const dataVersion = String(markerText).trim();
  if (!parseVersion(dataVersion)) return { action: "refuse", reason: "unreadable" };
  if (compareVersions(dataVersion, runningVersion) > 0) {
    return { action: "refuse", reason: "newer", dataVersion };
  }
  return { action: "proceed" };
}

/** The marker's text, null when it does not exist; any other read failure is unreadable. */
function readMarker(markerPath) {
  try {
    return { text: fs.readFileSync(markerPath, "utf8") };
  } catch (e) {
    if (e && e.code === "ENOENT") return { text: null };
    return { error: e };
  }
}

function checkDataVersion(markerPath, runningVersion) {
  const read = readMarker(markerPath);
  if (read.error) return { action: "refuse", reason: "unreadable" };
  return dataVersionDecision(read.text, runningVersion);
}

/** The dialog for a refusal: plain words, one way forward, and Quit. */
function refusalDialog(decision, runningVersion, markerPath) {
  const buttons = ["Download the latest version", "Quit"];
  if (decision.reason === "newer") {
    return {
      type: "warning",
      title: "Celerp",
      message:
        `Your data was last opened with Celerp ${decision.dataVersion}, which is newer than ` +
        `this copy (${runningVersion}). Download the latest version to continue.`,
      detail: "Your data has not been changed.",
      buttons,
      defaultId: 0,
      cancelId: 1,
    };
  }
  return {
    type: "warning",
    title: "Celerp",
    message:
      "Celerp could not tell which version last opened your data, so it has not opened it. " +
      "Your data has not been changed.",
    detail:
      `The version is recorded in:\n${markerPath}\n\n` +
      "If you may have used a newer Celerp on this computer, download the latest version. " +
      "If not, delete that one file and open Celerp again.",
    buttons,
    defaultId: 0,
    cancelId: 1,
  };
}

/**
 * Run the check before anything touches the data. Returns true to continue starting;
 * on a refusal it shows the dialog, opens the download page when asked, and returns
 * false so the caller quits. Never writes or deletes anything.
 */
function guardDataVersion({ markerPath, runningVersion, dialog, shell }) {
  const decision = checkDataVersion(markerPath, runningVersion);
  if (decision.action === "proceed") return true;
  const choice = dialog.showMessageBoxSync(refusalDialog(decision, runningVersion, markerPath));
  if (choice === 0) shell.openExternal(DOWNLOAD_URL);
  return false;
}

/** Record the version that ran here. Written whole (temp file + rename), never half. */
function writeMarker(markerPath, version) {
  const tmp = path.join(path.dirname(markerPath), `${MARKER_NAME}.tmp`);
  fs.writeFileSync(tmp, version);
  fs.renameSync(tmp, markerPath);
}

module.exports = {
  MARKER_NAME,
  DOWNLOAD_URL,
  parseVersion,
  compareVersions,
  dataVersionDecision,
  checkDataVersion,
  refusalDialog,
  guardDataVersion,
  writeMarker,
};
