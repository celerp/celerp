// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// The refusal screen (assets/refusal.js) driven through a stand-in page and preload
// bridge: Update Celerp runs the app's updater and ends at Restart to install.
const fs = require("fs");
const path = require("path");
const { screenFor, showRefusal } = require("../assets/refusal");

const VIEW = {
  message: "Your data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.3).",
  detail: "Your data has not been changed. Update Celerp to open it.",
  update: "Update Celerp", restart: "Restart to install", download: "Download the installer",
  downloadUrl: "https://celerp.com/download", quit: "Quit", checking: "Checking...",
  downloading: "Downloading... {pct}%", ready: "v{version} ready to install",
  noUpdate: "No newer version is available yet.",
  notReleased: "Your data has not been changed. Celerp can open it once version 2.6.0 or newer is available.",
  checkAgain: "Check again", restarting: "Restarting...",
  installing: "Installing v{version}. Celerp will be back in about a minute.",
  failed: { check: "Celerp couldn't check for updates.", stopped: "The update stopped before it finished.",
            inactive: "This copy of Celerp cannot update itself." },
};
const IDLE = { status: "idle", version: "", percent: 0, checking: false, upToDate: false, installing: false, revision: 0 };

function page() {
  const elements = {};
  const doc = {
    getElementById: (id) => {
      elements[id] ??= { id, textContent: "", hidden: id === "refusal", disabled: false, href: "", clicks: [],
                         addEventListener(type, fn) { this.clicks.push(fn); },
                         // A disabled button takes no click, as in a browser.
                         click() { if (!this.disabled) this.clicks.forEach((fn) => fn()); } };
      return elements[id];
    },
  };
  const calls = [];
  const on = {};
  const celerp = {
    getUpdateState: async () => IDLE,
    checkForUpdates: () => calls.push("check"),
    installUpdate: () => calls.push("install"),
    quit: () => calls.push("quit"),
  };
  for (const name of ["onUpdateLog", "onUpdateAvailable", "onDownloadProgress", "onUpdateDownloaded",
                      "onUpdateError", "onUpdateNotAvailable"]) {
    celerp[name] = (fn) => { on[name] = fn; };
  }
  return { doc, el: doc.getElementById, celerp, calls, on };
}

test("the screen says what happened and offers Update Celerp, the installer and Quit",
  async function test_refusal_screen_shows() {
    const p = page();
    await showRefusal(p.doc, p.celerp, VIEW);
    expect(p.el("loading").hidden).toBe(true);
    expect(p.el("refusal").hidden).toBe(false);
    expect(p.el("refusal-message").textContent).toBe(VIEW.message);
    expect(p.el("refusal-detail").textContent).toBe(VIEW.detail);
    // Nothing has been checked yet, so the status line claims nothing.
    expect(p.el("status").textContent).toBe("");
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Update Celerp", disabled: false });
    expect(p.el("refusal-download")).toMatchObject({ textContent: VIEW.download, href: VIEW.downloadUrl });
    p.el("refusal-quit").click();
    expect(p.calls).toEqual(["quit"]);
  });

test("Update Celerp checks, follows the download and installs on Restart to install",
  async function test_refusal_screen_updates() {
    const p = page();
    await showRefusal(p.doc, p.celerp, VIEW);
    p.el("refusal-update").click();
    expect(p.calls).toEqual(["check"]);
    expect(p.el("refusal-update").disabled).toBe(true);
    expect(p.el("status").textContent).toBe("Checking...");
    p.on.onDownloadProgress({ status: "downloading", version: "2.6.0", percent: 41.6, checking: false, revision: 3 });
    expect(p.el("status").textContent).toBe("Downloading... 42%");
    expect(p.el("refusal-update").disabled).toBe(true);
    p.on.onUpdateDownloaded({ status: "downloaded", version: "2.6.0", percent: 100, checking: false, revision: 4 });
    expect(p.el("status").textContent).toBe("v2.6.0 ready to install");
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Restart to install", disabled: false });
    p.el("refusal-update").click();
    // One click: the button waits and says so, as the update card's does.
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Restarting...", disabled: true });
    expect(p.el("status").textContent).toBe("Installing v2.6.0. Celerp will be back in about a minute.");
    p.el("refusal-update").click();
    expect(p.calls).toEqual(["check", "install"]);
  });

test("nothing newer says so, stops promising an update and offers Check again",
  async function test_refusal_screen_no_update() {
    const p = page();
    await showRefusal(p.doc, p.celerp, VIEW);
    p.el("refusal-update").click();
    p.on.onUpdateNotAvailable({ ...IDLE, upToDate: true, revision: 2 });
    expect(p.el("status").textContent).toBe(VIEW.noUpdate);
    expect(p.el("refusal-detail").textContent).toBe(VIEW.notReleased);
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Check again", disabled: false });
    p.el("refusal-update").click();
    expect(p.el("status").textContent).toBe("Checking...");
    expect(p.calls).toEqual(["check", "check"]);
  });

test("every failure says what failed and offers Update Celerp again", async function test_refusal_screen_failures() {
  const p = page();
  await showRefusal(p.doc, p.celerp, VIEW);
  for (const failure of ["check", "stopped", "inactive"]) {
    p.el("refusal-update").click();
    p.on.onUpdateError({ ...IDLE, status: "error", failure, revision: p.calls.length });
    expect([failure, p.el("status").textContent]).toEqual([failure, VIEW.failed[failure]]);
    expect(p.el("refusal-detail").textContent).toBe(VIEW.detail);
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Update Celerp", disabled: false });
  }
  // A failure with no line of its own is told as a failed check, never as nothing.
  p.on.onUpdateError({ ...IDLE, status: "error", failure: "download", revision: 9 });
  expect(p.el("status").textContent).toBe(VIEW.failed.check);
});

test("a page that loads again shows the state the main process kept", async function test_refusal_screen_reload() {
  for (const [state, status, label, disabled] of [
    [{ ...IDLE, checking: true, revision: 1 }, "Checking...", "Update Celerp", true],
    [{ ...IDLE, status: "downloading", version: "2.6.0", percent: 7, revision: 2 }, "Downloading... 7%", "Update Celerp", true],
    [{ ...IDLE, status: "error", failure: "stopped", revision: 3 }, VIEW.failed.stopped, "Update Celerp", false],
    [{ ...IDLE, upToDate: true, revision: 4 }, VIEW.noUpdate, "Check again", false],
    [{ ...IDLE, status: "downloaded", version: "2.6.0", installing: true, revision: 5 },
      "Installing v2.6.0. Celerp will be back in about a minute.", "Restarting...", true],
  ]) {
    const p = page();
    p.celerp.getUpdateState = async () => state;
    await showRefusal(p.doc, p.celerp, VIEW);
    expect([p.el("status").textContent, p.el("refusal-update").textContent, p.el("refusal-update").disabled])
      .toEqual([status, label, disabled]);
  }
});

test("an update already downloaded is replayed; an older replay never overwrites a newer state",
  async function test_refusal_screen_replay() {
    const p = page();
    p.celerp.getUpdateState = async () => ({ status: "downloaded", version: "2.6.0", percent: 100, revision: 7 });
    await showRefusal(p.doc, p.celerp, VIEW);
    expect(p.el("refusal-update").textContent).toBe("Restart to install");
    p.on.onUpdateLog({ ...IDLE, revision: 5 });
    expect(p.el("refusal-update").textContent).toBe("Restart to install");
  });

test("screenFor maps every updater state", function test_screen_for() {
  expect(screenFor(VIEW, IDLE)).toEqual(
    { detail: VIEW.detail, status: "", label: "Update Celerp", action: "update", busy: false });
  expect(screenFor(VIEW, { ...IDLE, upToDate: true, checking: true })).toEqual(
    { detail: VIEW.notReleased, status: "Checking...", label: "Check again", action: "update", busy: true });
  expect(screenFor(VIEW, { ...IDLE, status: "downloading", upToDate: false, percent: 41.6 }).status).toBe("Downloading... 42%");
});

test("loading.html carries the screen's elements and loads its script", function test_loading_page_elements() {
  const html = fs.readFileSync(path.join(__dirname, "..", "assets", "loading.html"), "utf8");
  for (const id of ["loading", "refusal", "refusal-message", "refusal-detail", "refusal-update", "refusal-download",
                    "refusal-quit", "status"]) {
    expect([id, html.includes(`id="${id}"`)]).toEqual([id, true]);
  }
  expect(html).toContain('<script src="refusal.js"></script>');
  expect(html).toMatch(/<div class="refusal" id="refusal" hidden>/);
});
