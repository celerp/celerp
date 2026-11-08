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
   * What the screen shows for the updater's state (update-state.js): the status
   * line, the main button's label and action ("update" checks, "restart" installs),
   * and whether that button waits. `notAvailable` is set when the user's check found
   * nothing newer. A failed check or download is told in the update failure dialog,
   * so the screen offers Update Celerp again.
   */
  function screenFor(view, state, notAvailable) {
    if (state.status === "downloaded") {
      return { status: fill(view.ready, { version: state.version }), label: view.restart, action: "restart", busy: false };
    }
    const update = { label: view.update, action: "update" };
    if (state.status === "downloading") {
      return { ...update, status: fill(view.downloading, { pct: Math.round(state.percent) }), busy: true };
    }
    if (state.checking) return { ...update, status: view.checking, busy: true };
    if (state.status === "idle" && notAvailable) return { ...update, status: view.noUpdate, busy: false };
    return { ...update, status: "", busy: false };
  }

  /** Show the screen for `view` in `doc`, driven by the preload bridge `celerp`. */
  function showRefusal(doc, celerp, view) {
    const el = (id) => doc.getElementById(id);
    let state = { status: "idle", checking: false };
    let notAvailable = false;
    let action = "update";

    function render() {
      const screen = screenFor(view, state, notAvailable);
      action = screen.action;
      el("status").textContent = screen.status;
      el("refusal-update").textContent = screen.label;
      el("refusal-update").disabled = screen.busy;
    }
    // A state older than the one shown (a replay that arrives after a live event) is dropped.
    const follow = (next) => {
      if ((next.revision ?? 0) < (state.revision ?? 0)) return;
      state = next;
      render();
    };

    el("refusal-message").textContent = view.message;
    el("refusal-detail").textContent = view.detail;
    el("refusal-download").textContent = view.download;
    el("refusal-download").href = view.downloadUrl;
    el("refusal-quit").textContent = view.quit;
    el("refusal-update").addEventListener("click", () => {
      if (action === "restart") {
        celerp.installUpdate();
        return;
      }
      follow({ ...state, checking: true });
      celerp.checkForUpdates();
    });
    el("refusal-quit").addEventListener("click", () => celerp.quit());
    celerp.onUpdateLog(follow);
    celerp.onUpdateAvailable(follow);
    celerp.onDownloadProgress(follow);
    celerp.onUpdateDownloaded(follow);
    celerp.onUpdateError(follow);
    celerp.onUpdateNotAvailable((next) => { notAvailable = true; follow(next); });
    el("loading").hidden = true;
    el("refusal").hidden = false;
    render();
    return celerp.getUpdateState().then(follow);
  }

  if (typeof module !== "undefined") module.exports = { screenFor, showRefusal };
  else root.showRefusal = (view) => showRefusal(root.document, root.celerp, view);
})(this);
