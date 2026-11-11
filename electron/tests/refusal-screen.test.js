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
  quit: "Quit", checking: "Checking...",
  downloading: "Downloading... {pct}%", ready: "v{version} ready to install",
  noUpdate: "No newer version is available yet.",
  notReleased: "Your data has not been changed. Celerp can open it once version 2.6.0 or newer is available.",
  checkAgain: "Check again", restarting: "Restarting...",
  tooOld: "Celerp {version} is the newest release, and it cannot open this data yet.",
  installing: "Installing v{version}. Celerp will be back in about a minute.",
  relaunch: "Restart Now",
  relaunchDetail: "Your data has not been changed. Restart Celerp to check for a version that can open it.",
  failed: { check: "Celerp couldn't check for updates.", stopped: "The update stopped before it finished.",
            inactive: "This copy of Celerp cannot update itself." },
};
const IDLE = { status: "idle", version: "", percent: 0, checking: false, upToDate: false, installing: false, revision: 0 };

function page() {
  const elements = {};
  const doc = {
    getElementById: (id) => {
      elements[id] ??= { id, textContent: "", hidden: id === "refusal", disabled: false, clicks: [],
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
    // As the preload bridge does: a promise that settles once main's check is done.
    checkForUpdates: async () => { calls.push("check"); },
    installUpdate: () => calls.push("install"),
    quit: () => calls.push("quit"),
    restartApp: () => calls.push("relaunch"),
  };
  for (const name of ["onUpdateLog", "onUpdateAvailable", "onDownloadProgress", "onUpdateDownloaded",
                      "onUpdateError", "onUpdateNotAvailable"]) {
    celerp[name] = (fn) => { on[name] = fn; };
  }
  return { doc, el: doc.getElementById, celerp, calls, on };
}

// Let every pending promise of the screen settle (a check that ended, the state read after it).
const settle = () => new Promise((resolve) => setImmediate(resolve));

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
    expect(p.el("refusal-download")).toMatchObject({ textContent: VIEW.download });
    expect(p.el("refusal-quit").textContent).toBe(VIEW.quit);
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
    await settle();
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
    await settle();
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
  expect(screenFor(VIEW, { ...IDLE, status: "too-old", version: "2.5.9" })).toEqual(
    { detail: VIEW.notReleased, status: "Celerp 2.5.9 is the newest release, and it cannot open this data yet.",
      label: "Check again", action: "update", busy: false });
  expect(screenFor(VIEW, { ...IDLE, status: "too-old", version: "2.5.9", checking: true })).toMatchObject(
    { status: "Checking...", label: "Check again", action: "update", busy: true });
});

test("every state of the screen that is not busy has a way forward: a check, an install or a restart",
  function test_every_state_has_an_action() {
    const states = [
      IDLE, { ...IDLE, upToDate: true }, { ...IDLE, status: "too-old", version: "2.5.9" },
      { ...IDLE, status: "relaunch" }, { ...IDLE, status: "downloaded", version: "2.6.0" },
      ...Object.keys(VIEW.failed).map((failure) => ({ ...IDLE, status: "error", failure })),
    ];
    for (const state of states) {
      const screen = screenFor(VIEW, state);
      expect([state.status, screen.busy, Boolean(screen.label), ["update", "restart", "relaunch"].includes(screen.action)])
        .toEqual([state.status, false, true, true]);
    }
  });

test("loading.html carries the screen's elements and loads its script", function test_loading_page_elements() {
  const html = fs.readFileSync(path.join(__dirname, "..", "assets", "loading.html"), "utf8");
  for (const id of ["loading", "refusal", "refusal-message", "refusal-detail", "refusal-update", "refusal-download",
                    "refusal-quit", "status"]) {
    expect([id, html.includes(`id="${id}"`)]).toEqual([id, true]);
  }
  expect(html).toContain('<a id="refusal-download" href="https://celerp.com/download"></a>');
  expect(html).toContain('<script src="refusal.js"></script>');
  expect(html).toMatch(/<div class="refusal" id="refusal" hidden>/);
});

// What the screen shows once the updater has found a release (update-state.js decides whether
// it opens the data, which 2.6.0 opened): "downloading" for one it downloads, "too-old" otherwise.
async function foundRelease(version, status) {
  const p = page();
  await showRefusal(p.doc, p.celerp, VIEW);
  p.el("refusal-update").click();
  p.on.onUpdateAvailable({ ...IDLE, status, version, percent: 0, revision: 2 });
  await settle();
  return p;
}

test("a release that cannot open the data is not offered: the screen says no release opens it yet and offers Check again",
  async function test_older_release_is_not_offered() {
    const p = await foundRelease("2.5.9", "too-old");
    expect(p.el("refusal-detail").textContent).toBe(VIEW.notReleased);
    expect(p.el("status").textContent).toBe("Celerp 2.5.9 is the newest release, and it cannot open this data yet.");
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Check again", hidden: false, disabled: false });
    p.el("refusal-update").click();
    expect(p.calls).toEqual(["check", "check"]);
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Check again", disabled: true });
    expect(p.el("status").textContent).toBe("Checking...");
    // Quit stays.
    expect(p.el("refusal-quit").hidden).toBe(false);
  });

test("a release being downloaded is offered as the way out, and ends at Restart to install",
  async function test_downloading_release_is_offered() {
    const p = await foundRelease("2.6.0", "downloading");
    expect(p.el("refusal-detail").textContent).toBe(VIEW.detail);
    p.on.onUpdateDownloaded({ ...IDLE, status: "downloaded", version: "2.6.0", percent: 100, revision: 3 });
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Restart to install", hidden: false, disabled: false });
    p.el("refusal-update").click();
    expect(p.calls).toEqual(["check", "install"]);
  });

test("updater state from before the refusal offers one action: Celerp restarts and checks, once",
  async function test_relaunch_is_offered() {
    const p = page();
    p.celerp.getUpdateState = async () => ({ ...IDLE, status: "relaunch", revision: 4 });
    await showRefusal(p.doc, p.celerp, VIEW);
    expect([p.el("refusal-detail").textContent, p.el("status").textContent]).toEqual([VIEW.relaunchDetail, ""]);
    expect(p.el("refusal-update")).toMatchObject({ textContent: VIEW.relaunch, hidden: false, disabled: false });
    p.el("refusal-update").click();
    p.el("refusal-update").click();
    expect(p.calls).toEqual(["relaunch"]);
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Restarting...", disabled: true });
  });

// A check main answers only through checkForUpdates' promise (a check it ignores sends no event).
function pendingCheck(p) {
  const check = {};
  p.celerp.checkForUpdates = () => {
    p.calls.push("check");
    return new Promise((resolve, reject) => Object.assign(check, { resolve, reject }));
  };
  return check;
}

test("Restart Now stays Restarting... whatever main sends after the click, until the process exits",
  async function test_refusal_screen_relaunching_survives_later_events() {
    const p = page();
    p.celerp.getUpdateState = async () => ({ ...IDLE, status: "relaunch", revision: 4 });
    await showRefusal(p.doc, p.celerp, VIEW);
    p.el("refusal-update").click();
    p.on.onUpdateLog({ ...IDLE, status: "relaunch", revision: 5 });
    await settle();
    expect(p.el("refusal-update")).toMatchObject({ textContent: "Restarting...", disabled: true });
    p.el("refusal-update").click();
    expect(p.calls).toEqual(["relaunch"]);
  });

test("Check again ends when the check settles, even when main sends nothing (a withdrawn release)",
  async function test_refusal_screen_check_again_ends_when_check_settles() {
    for (const ending of ["resolve", "reject"]) {
      const p = page();
      p.celerp.getUpdateState = async () => ({ ...IDLE, status: "too-old", version: "2.5.9", revision: 3 });
      await showRefusal(p.doc, p.celerp, VIEW);
      const check = pendingCheck(p);
      p.el("refusal-update").click();
      await settle();
      expect([ending, p.el("status").textContent, p.el("refusal-update").disabled]).toEqual([ending, "Checking...", true]);
      // Main found nothing newer: the release was withdrawn, so its state reads as up to date.
      p.celerp.getUpdateState = async () => ({ ...IDLE, upToDate: true, revision: 5 });
      check[ending](new Error("check failed"));
      await settle();
      expect([ending, p.el("status").textContent, p.el("refusal-update").textContent, p.el("refusal-update").disabled])
        .toEqual([ending, VIEW.noUpdate, "Check again", false]);
      // Quit stays usable throughout.
      p.el("refusal-quit").click();
      expect(p.calls).toEqual(["check", "quit"]);
    }
  });

test("every busy state a button sets ends on its own signal and survives unrelated events",
  async function test_every_refusal_intent_ends_and_survives_events() {
    const intents = [
      // [start state, label while busy, an unrelated event, the busy state's own end (null: the process exits)]
      [{ ...IDLE, status: "relaunch", revision: 2 }, "Restarting...",
        { ...IDLE, status: "relaunch", revision: 3 }, null],
      [{ ...IDLE, status: "downloaded", version: "2.6.0", percent: 100, revision: 2 }, "Restarting...",
        { ...IDLE, status: "downloaded", version: "2.6.0", percent: 100, revision: 3 },
        { event: { ...IDLE, status: "error", failure: "stopped", downloadFailed: true, version: "2.6.0", revision: 4 } }],
      [{ ...IDLE, revision: 2 }, "Update Celerp", { ...IDLE, revision: 3 }, { settles: true }],
    ];
    for (const [start, busyLabel, unrelated, end] of intents) {
      const p = page();
      p.celerp.getUpdateState = async () => start;
      await showRefusal(p.doc, p.celerp, VIEW);
      const check = pendingCheck(p);
      p.el("refusal-update").click();
      p.on.onUpdateLog(unrelated);
      await settle();
      expect([start.status, p.el("refusal-update").textContent, p.el("refusal-update").disabled])
        .toEqual([start.status, busyLabel, true]);
      if (!end) continue;
      if (end.event) p.on.onUpdateError(end.event);
      if (end.settles) check.resolve();
      await settle();
      expect([start.status, p.el("refusal-update").disabled]).toEqual([start.status, false]);
    }
  });
