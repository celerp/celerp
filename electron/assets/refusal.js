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
   * installs), whether that button waits, and whether it shows at all (offer). Every
   * state says where it stands: a failure names what failed and offers Update Celerp
   * again, once nothing newer is found the screen stops promising an update and offers
   * Check again, and a release that cannot open the data, found, downloading or downloaded
   * (the judged state "too-old", update-state.js), is never offered: the screen says no
   * release opens it yet, leaving the installer page and Quit.
   */
  function screenFor(view, state) {
    if (state.status === "too-old") {
      return { detail: view.notReleased, status: fill(view.tooOld, { version: state.version }), label: view.update,
               action: "update", busy: true, offer: false };
    }
    return { ...offered(view, state), offer: true };
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
   */
  function showRefusal(doc, celerp, view) {
    const el = (id) => doc.getElementById(id);
    let state = { status: "idle", checking: false };
    let action = "update";

    function render() {
      const screen = screenFor(view, state);
      action = screen.action;
      el("refusal-detail").textContent = screen.detail;
      el("status").textContent = screen.status;
      el("refusal-update").textContent = screen.label;
      el("refusal-update").disabled = screen.busy;
      el("refusal-update").hidden = !screen.offer;
    }
    // A state older than the one shown (a replay that arrives after a live event) is dropped.
    const follow = (next) => {
      if ((next.revision ?? 0) < (state.revision ?? 0)) return;
      state = next;
      render();
    };

    el("refusal-message").textContent = view.message;
    el("refusal-download").textContent = view.download;
    el("refusal-download").href = view.downloadUrl;
    el("refusal-quit").textContent = view.quit;
    el("refusal-update").addEventListener("click", () => {
      if (action === "restart") {
        follow({ ...state, installing: true });
        celerp.installUpdate();
        return;
      }
      follow({ ...state, checking: true });
      celerp.checkForUpdates();
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
