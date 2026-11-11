// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The screen an older copy of Celerp shows on data a newer Celerp has opened: what
// happened, and Update Celerp with the app's own updater. loading.html runs it with
// the strings app-main.js sends (compatibility.js refusalView); the tests require it.
(function (root) {
  "use strict";

  function fill(text, values) {
    return text.replace(/\{(\w+)\}/g, (whole, key) => (key in values ? String(values[key]) : whole));
  }

  /**
   * What the screen shows for the updater's state (update-state.js): the detail line,
   * the status line, the main button's label and action ("update" checks, "restart"
   * installs, "relaunch" restarts Celerp), and whether that button waits. Every state says
   * where it stands and has a way forward: a failure names what failed and offers Update
   * Celerp again, and once nothing newer is found, or only a release that cannot open the
   * data (the judged state "too-old", update-state.js, the result of a check under the
   * version rule), the screen stops promising an update and offers Check again. A release
   * that cannot open the data is never offered. When the updater was used before the rule
   * ("relaunch"), the one offer is a restart, which checks again in a new process.
   */
  function screenFor(view, state) {
    if (state.status === "relaunch") {
      return { detail: view.relaunchDetail, status: "", label: state.relaunching ? view.restarting : view.relaunch,
               action: "relaunch", busy: Boolean(state.relaunching) };
    }
    if (state.status === "too-old") {
      return { detail: view.notReleased, status: state.checking ? view.checking : fill(view.tooOld, { version: state.version }),
               label: view.checkAgain, action: "update", busy: Boolean(state.checking) };
    }
    return offered(view, state);
  }

  function offered(view, state) {
    if (state.status === "downloaded") {
      return state.installing
        ? { detail: view.detail, status: fill(view.installing, { version: state.version }), label: view.restarting,
            action: "restart", busy: true }
        : { detail: view.detail, status: fill(view.ready, { version: state.version }), label: view.restart,
            action: "restart", busy: false };
    }
    const update = { detail: state.upToDate ? view.notReleased : view.detail,
                     label: state.upToDate ? view.checkAgain : view.update, action: "update" };
    if (state.status === "downloading") {
      return { ...update, status: fill(view.downloading, { pct: Math.round(state.percent) }), busy: true };
    }
    if (state.checking) return { ...update, status: view.checking, busy: true };
    if (state.status === "error") return { ...update, status: view.failed[state.failure] ?? view.failed.check, busy: false };
    return { ...update, status: state.upToDate ? view.noUpdate : "", busy: false };
  }

  /**
   * Show the screen for `view` in `doc`, driven by the preload bridge `celerp`. The
   * state is the main process's (update-state.js), replayed on every load, so a page
   * that loads again shows where the update stands; a click shows its result at once.
   * What a click started (`intent`) is kept apart from main's state, so no event from main
   * can drop it, and each ends on its own signal: checking when the check settles,
   * installing when the state leaves "downloaded", relaunching never (the process exits).
   */
  function showRefusal(doc, celerp, view) {
    const el = (id) => doc.getElementById(id);
    let state = { status: "idle", checking: false };
    let intent = "";
    let action = "update";

    function render() {
      const screen = screenFor(view, { ...state, relaunching: intent === "relaunching",
                                       installing: state.installing || intent === "installing",
                                       checking: state.checking || intent === "checking" });
      action = screen.action;
      el("refusal-detail").textContent = screen.detail;
      el("status").textContent = screen.status;
      el("refusal-update").textContent = screen.label;
      el("refusal-update").disabled = screen.busy;
    }
    // A state older than the one shown (a replay that arrives after a live event) is dropped.
    const follow = (next) => {
      if ((next.revision ?? 0) < (state.revision ?? 0)) return;
      state = next;
      if (intent === "installing" && state.status !== "downloaded") intent = "";
      render();
    };
    const start = (next) => {
      intent = next;
      render();
    };

    el("refusal-message").textContent = view.message;
    el("refusal-download").textContent = view.download;
    el("refusal-download").href = view.downloadUrl;
    el("refusal-quit").textContent = view.quit;
    el("refusal-update").addEventListener("click", () => {
      if (action === "relaunch") {
        start("relaunching");
        celerp.restartApp();
        return;
      }
      if (action === "restart") {
        start("installing");
        celerp.installUpdate();
        return;
      }
      start("checking");
      // Main answers a check it ignores with no event, so the check's own end ends checking.
      new Promise((resolve) => resolve(celerp.checkForUpdates())).catch(() => {}).then(() => {
        if (intent === "checking") start("");
        return celerp.getUpdateState();
      }).then(follow);
    });
    el("refusal-quit").addEventListener("click", () => celerp.quit());
    for (const on of ["onUpdateLog", "onUpdateAvailable", "onDownloadProgress", "onUpdateDownloaded", "onUpdateError",
                      "onUpdateNotAvailable"]) {
      celerp[on](follow);
    }
    el("loading").hidden = true;
    el("refusal").hidden = false;
    render();
    return celerp.getUpdateState().then(follow);
  }

  if (typeof module !== "undefined") module.exports = { screenFor, showRefusal };
  else root.showRefusal = (view) => showRefusal(root.document, root.celerp, view);
})(this);
