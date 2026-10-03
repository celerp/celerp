// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// Whether this copy of Celerp may open the database, decided by the database itself.
//
// The decision is `python -m celerp compatibility --db-url <url>`, the same check
// every other way of opening the database makes: it refuses data a newer Celerp has
// opened, or whose schema this copy does not know, and changes nothing. Startup runs
// it before anything writes to the data directory, the config or the database, and
// stops with a plain message when it refuses.
//
// Pure apart from the injected runner, dialog and shell, so it is unit-testable
// without Electron, like restart.js.

const DOWNLOAD_URL = "https://celerp.com/download";
// `celerp compatibility` exits with this when this copy must not open the database.
const REFUSED_EXIT = 3;

function compatibilityArgs(dbUrl) {
  return ["-m", "celerp", "compatibility", "--db-url", dbUrl];
}

/**
 * The decision from the check's result ({ status, stdout, stderr, error }, as
 * spawnSync returns it). Compatible: { ok: true }. Refused: { ok: false, status,
 * running, recorded }. A check that could not run is an error, never a pass.
 */
function readDecision(result) {
  if (result.status === 0 || result.status === REFUSED_EXIT) {
    let decision;
    try {
      decision = JSON.parse(String(result.stdout).trim().split("\n").pop());
    } catch {
      decision = null;
    }
    if (decision && typeof decision.status === "string") {
      return { ...decision, ok: result.status === 0 };
    }
  }
  const why = (result.error && result.error.message) || String(result.stderr || "").trim().split("\n").pop();
  throw new Error(`Celerp could not check your data before opening it, so it has not opened it. ${why || ""}`.trim());
}

/** The dialog for a refusal: plain words, one way forward, and Quit. */
function refusalDialog(decision) {
  const message = decision.status === "newer_app"
    ? `Your data was last opened with Celerp ${decision.recorded}, which is newer than ` +
      `this copy (${decision.running}). Download the latest version to continue.`
    : `Your data cannot safely be opened by this copy of Celerp (${decision.running}). ` +
      "Download the latest version to continue.";
  return {
    type: "warning",
    title: "Celerp",
    message,
    detail: "Your data has not been changed.",
    buttons: ["Download the latest version", "Quit"],
    defaultId: 0,
    cancelId: 1,
  };
}

/**
 * Run the check. Returns true when this copy may open the database; on a refusal it
 * shows the dialog, opens the download page when asked, and returns false so the
 * caller quits. Throws when the check itself could not run.
 */
function mayOpenData({ check, dialog, shell }) {
  const decision = readDecision(check());
  if (decision.ok) return true;
  if (dialog.showMessageBoxSync(refusalDialog(decision)) === 0) shell.openExternal(DOWNLOAD_URL);
  return false;
}

module.exports = { DOWNLOAD_URL, REFUSED_EXIT, compatibilityArgs, readDecision, refusalDialog, mayOpenData };
