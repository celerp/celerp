// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// Whether this copy of Celerp may open the database, decided by the database itself.
//
// The decision is `python -m celerp compatibility --db-url <url>`, the same check
// every other way of opening the database makes: it refuses data a newer Celerp has
// opened, or whose schema this copy does not know, and changes nothing. Startup runs
// it before any change to application data, the database or the modules; when it
// refuses, the user is told why and offered the update that opens the data.
//
// Pure apart from the injected runner, dialog, shell and callbacks, so it is unit-testable
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

// Refusals an update resolves: a newer Celerp has opened the data. An unreadable
// version record stays unreadable to every version, so updating does not help.
const UPDATABLE = new Set(["newer_app", "unknown_schema"]);
const HELP_URL = "https://github.com/celerp/celerp/issues";

// Every way an update can fail (update-state.js failure), each with its line on the screen.
const UPDATE_FAILURES = ["check", "download", "save", "verify", "install", "stopped", "inactive"];

/**
 * The words of the refusal screen (assets/refusal.js) for an updatable refusal, in
 * the user's language (`t`, as locale.js translator returns). On Windows (`windows`)
 * the update installs in a setup window, which takes longer. `recorded` is the version
 * that opened the data, from the check's decision: the updater downloads only a release
 * at least that new (empty for an unknown schema, which records no newer version).
 */
function refusalView(decision, t, windows) {
  const newer = decision.status === "newer_app";
  return {
    message: newer
      ? t("shell.data_newer", { recorded: decision.recorded, running: decision.running })
      : t("shell.data_newer_schema", { running: decision.running }),
    detail: t("shell.data_update_to_open"),
    update: t("btn.update_celerp"),
    restart: t("btn.restart_to_install"),
    download: t("btn.download_installer"),
    downloadUrl: DOWNLOAD_URL,
    quit: t("btn.quit"),
    checking: t("shell.checking"),
    downloading: t("shell.js_downloading_pct"),
    ready: t("shell.js_version_ready"),
    noUpdate: t("shell.data_no_update"),
    notReleased: newer
      ? t("shell.data_not_released", { recorded: decision.recorded })
      : t("shell.data_not_released_schema"),
    recorded: newer ? decision.recorded : "",
    tooOld: t("shell.data_release_too_old"),
    checkAgain: t("btn.check_again"),
    restarting: t("shell.restarting"),
    installing: t(windows ? "shell.update_installing_windows" : "shell.update_installing"),
    failed: Object.fromEntries(UPDATE_FAILURES.map((failure) => [failure, t(`shell.update_${failure}_failed`)])),
  };
}

/** The dialog for an unreadable version record: what happened, Get help, and Quit. */
function invalidRecordDialog(t) {
  return {
    type: "warning",
    title: "Celerp",
    message: t("shell.data_invalid_record"),
    buttons: [t("shell.update_get_help"), t("btn.quit")],
    defaultId: 0,
    cancelId: 1,
  };
}

/**
 * Run the check. Returns true when this copy may open the database. On a refusal
 * it stops the database (`stop`) and returns false: data a newer Celerp opened gets
 * the refusal screen (`offerUpdate(view)`) and Celerp stays open so the user can
 * update it; an unreadable record gets its dialog, then `quit`. Throws when the
 * check itself could not run.
 */
async function mayOpenData({ check, t, windows, stop, offerUpdate, dialog, shell, quit }) {
  const decision = readDecision(check());
  if (decision.ok) return true;
  await stop();
  if (UPDATABLE.has(decision.status)) {
    await offerUpdate(refusalView(decision, t, windows));
  } else {
    const { response } = await dialog.showMessageBox(invalidRecordDialog(t));
    if (response === 0) await shell.openExternal(HELP_URL);
    quit();
  }
  return false;
}

module.exports = {
  DOWNLOAD_URL, HELP_URL, REFUSED_EXIT, UPDATE_FAILURES, compatibilityArgs, readDecision, refusalView, invalidRecordDialog, mayOpenData,
};
