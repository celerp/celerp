// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const { EventEmitter } = require("events");
const { compareVersions, trackUpdater, serveUpdateState } = require("../update-state");

// A fake electron-updater: the tracker only subscribes to its events.
function setup() {
  const updater = new EventEmitter();
  const sent = [];
  const { getUpdateState } = trackUpdater(updater, (channel, payload) => sent.push([channel, payload]));
  return { updater, sent, getUpdateState };
}

function downloadedSetup() {
  const t = setup();
  t.updater.emit("update-available", { version: "2.0.1" });
  t.updater.emit("download-progress", { percent: 55 });
  t.updater.emit("update-downloaded", { version: "2.0.1" });
  t.sent.length = 0;
  return t;
}

// The state without its log lines and revision.
const stateOf = ({ log, revision, ...state }) => state;

const IDLE = { status: "idle", version: "", percent: 0, message: "", checking: false, downloadFailed: false, failure: "",
               releaseUrl: "", upToDate: false, installing: false };
// What a check that found nothing newer leaves.
const UP_TO_DATE = { ...IDLE, upToDate: true };
const DOWNLOADED = { ...IDLE, status: "downloaded", version: "2.0.1", percent: 100 };
const LATEST = "https://github.com/celerp/celerp/releases/latest";

test("a fresh tracker starts idle", function test_new_tracker_starts_idle() {
  const { getUpdateState } = setup();
  expect(getUpdateState()).toEqual({ ...IDLE, log: [], revision: 0 });
});

test("relaunch starts fresh: a new tracker is idle even after another reached downloaded",
  function test_second_tracker_starts_idle_after_downloaded() {
    const first = downloadedSetup();
    expect(first.getUpdateState().status).toBe("downloaded");
    const second = setup();
    expect(second.getUpdateState()).toEqual({ ...IDLE, log: [], revision: 0 });
  });

test("update found goes straight to downloading and is forwarded", function test_update_found_starts_downloading() {
  const { updater, sent, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  const state = { ...IDLE, status: "downloading", version: "2.0.1" };
  expect(stateOf(getUpdateState())).toEqual(state);
  expect(sent.map(([ch, s]) => [ch, stateOf(s)])).toEqual([["update-available", state]]);
});

test("progress updates the percent and keeps the version", function test_progress_updates_percent() {
  const { updater, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  updater.emit("download-progress", { percent: 12.5 });
  expect(stateOf(getUpdateState())).toEqual({ ...IDLE, status: "downloading", version: "2.0.1", percent: 12.5 });
  updater.emit("download-progress", { percent: 80 });
  expect(getUpdateState().percent).toBe(80);
});

test("a progress percent that is not 0 to 100 is kept in range, and one that is not a number is 0",
  function test_progress_percent_is_validated() {
    const cases = [["55", 55], [-3, 0], ["abc", 0], [150, 100], [null, 0], [Infinity, 0]];
    for (const [given, stored] of cases) {
      const { updater, getUpdateState } = setup();
      updater.emit("update-available", { version: "2.0.1" });
      updater.emit("download-progress", { percent: given });
      expect([given, getUpdateState().percent]).toEqual([given, stored]);
    }
  });

test("state carries version, percent and error message", function test_state_carries_version_percent_message() {
  const { updater, getUpdateState } = setup();
  updater.emit("update-available", { version: "3.1.0" });
  updater.emit("download-progress", { percent: 40 });
  expect(getUpdateState()).toMatchObject({ version: "3.1.0", percent: 40 });
  updater.emit("error", new Error("disk full"));
  expect(getUpdateState()).toMatchObject({ status: "error", version: "3.1.0", message: "disk full" });
  const done = downloadedSetup();
  expect(stateOf(done.getUpdateState())).toEqual(DOWNLOADED);
});

test("downloaded cannot be demoted by later updater noise", function test_downloaded_cannot_be_demoted() {
  const noise = [
    ["update-not-available", {}],
    ["download-progress", { percent: 3 }],
    ["error", new Error("net::ERR_INTERNET_DISCONNECTED")],
    ["update-available", { version: "2.0.2" }],
    ["checking-for-update"],
  ];
  for (const [event, payload] of noise) {
    const { updater, sent, getUpdateState } = downloadedSetup();
    updater.emit(event, payload);
    expect(stateOf(getUpdateState())).toEqual(DOWNLOADED);
    // Anything still sent is a log line on the unchanged downloaded state.
    for (const [, s] of sent) expect(stateOf(s)).toEqual(DOWNLOADED);
  }
});

test("an error before any download is retained for later replay", function test_pre_download_error_is_retained() {
  const { updater, sent, getUpdateState } = setup();
  updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
  const state = { ...IDLE, status: "error", message: "getaddrinfo ENOTFOUND github.com", failure: "check", releaseUrl: LATEST,
                  log: ["Update error: getaddrinfo ENOTFOUND github.com"], revision: 1 };
  expect(getUpdateState()).toEqual(state);
  // Reading it again (a later page load) still returns the error.
  expect(getUpdateState()).toEqual(state);
  expect(sent).toEqual([["update-error", state]]);
});

test("an error during a download is retained with the version", function test_error_during_download_is_retained() {
  const { updater, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  updater.emit("download-progress", { percent: 30 });
  updater.emit("error", "sha512 checksum mismatch");
  expect(getUpdateState()).toMatchObject({ status: "error", version: "2.0.1", message: "sha512 checksum mismatch" });
});

test("getUpdateState returns a copy the caller cannot use to change the state",
  function test_get_update_state_is_read_only() {
    const { updater, getUpdateState } = setup();
    updater.emit("update-available", { version: "2.0.1" });
    const snapshot = getUpdateState();
    try { snapshot.status = "idle"; } catch (_) { /* frozen copies may throw in strict mode */ }
    snapshot.log.push("injected");
    expect(getUpdateState().status).toBe("downloading");
    expect(getUpdateState().log).not.toContain("injected");
  });

test("every progress tick is forwarded to the window", function test_every_progress_tick_is_forwarded() {
  const { updater, sent } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  for (let p = 1; p <= 20; p++) updater.emit("download-progress", { percent: p });
  const ticks = sent.filter(([ch]) => ch === "download-progress");
  expect(ticks.map(([, s]) => s.percent)).toEqual(Array.from({ length: 20 }, (_, i) => i + 1));
});

test("an updater error is forwarded as update-error, never as not-available",
  function test_error_is_forwarded_as_update_error() {
    const { updater, sent } = setup();
    updater.emit("error", new Error("boom"));
    expect(sent.map(([ch]) => ch)).toEqual(["update-error"]);
  });

test("an error with no usable message is stored with an empty message",
  function test_error_without_message_is_stored_empty() {
    for (const err of [undefined, null, {}, new Error("")]) {
      const { updater, getUpdateState } = setup();
      updater.emit("error", err);
      expect(getUpdateState()).toEqual({ ...IDLE, status: "error", failure: "check", releaseUrl: LATEST, log: ["Update error"], revision: 1 });
    }
  });

test("a check that finds nothing does not stop a download in progress",
  function test_not_available_is_ignored_while_downloading() {
    const { updater, sent, getUpdateState } = setup();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("download-progress", { percent: 30 });
    sent.length = 0;
    updater.emit("update-not-available", {});
    expect(stateOf(getUpdateState())).toEqual({ ...IDLE, status: "downloading", version: "2.0.1", percent: 30 });
    expect(sent).toEqual([]);
  });

test("the log lines are part of the state, so a replay shows what the live events showed",
  function test_log_lines_are_replayed_with_the_state() {
    const { updater, sent, getUpdateState } = setup();
    updater.emit("checking-for-update");
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("download-progress", { percent: 4, bytesPerSecond: 2048 });
    updater.emit("download-progress", { percent: 7, bytesPerSecond: 2048 });
    updater.emit("download-progress", { percent: 23, bytesPerSecond: 4096 });
    updater.emit("update-downloaded", { version: "2.0.1" });
    const log = [
      "Checking for update...",
      "Found v2.0.1, downloading...",
      "Downloading: 4% (2 KB/s)",
      "Downloading: 23% (4 KB/s)",
      "v2.0.1 ready. Restart to install.",
    ];
    expect(getUpdateState().log).toEqual(log);
    // The last live event carried the same log the replay returns.
    expect(sent[sent.length - 1][1]).toEqual(getUpdateState());
  });

test("an error after the update downloaded is logged, and the state stays downloaded",
  function test_error_after_downloaded_is_logged() {
    const { updater, sent, getUpdateState } = downloadedSetup();
    updater.emit("error", new Error("net::ERR_INTERNET_DISCONNECTED"));
    expect(stateOf(getUpdateState())).toEqual(DOWNLOADED);
    expect(getUpdateState().log.slice(-1)).toEqual(["Update error: net::ERR_INTERNET_DISCONNECTED"]);
    expect(sent).toEqual([["update-error", getUpdateState()]]);
  });

test("a re-check after the update downloaded changes nothing, not even the log",
  function test_recheck_after_downloaded_keeps_the_log() {
    const { updater, sent, getUpdateState } = downloadedSetup();
    const ready = getUpdateState();
    expect(ready.log.slice(-1)).toEqual(["v2.0.1 ready. Restart to install."]);
    // The periodic re-check finds the same, already downloaded, update.
    updater.emit("checking-for-update");
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("update-downloaded", { version: "2.0.1" });
    expect(getUpdateState()).toEqual(ready);
    expect(sent).toEqual([]);
  });

test("a check in progress is part of the state until it has a result",
  function test_check_in_progress_is_in_the_state() {
    const { updater, sent, getUpdateState } = setup();
    updater.emit("checking-for-update");
    expect(stateOf(getUpdateState())).toEqual({ ...IDLE, checking: true });
    expect(sent).toEqual([["update-log", getUpdateState()]]);
    updater.emit("update-not-available", {});
    expect(stateOf(getUpdateState())).toEqual(UP_TO_DATE);
  });

test("a failed check is kept until a later check has a result, and stays in the log",
  function test_check_error_clears_to_the_next_check_result() {
    for (const [event, payload, result] of [
      ["update-not-available", {}, UP_TO_DATE],
      ["update-available", { version: "2.0.1" }, { ...IDLE, status: "downloading", version: "2.0.1" }],
    ]) {
      const { updater, getUpdateState } = setup();
      updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
      updater.emit("checking-for-update");
      // While the new check runs, the failure is still there.
      expect(stateOf(getUpdateState())).toEqual(
        { ...IDLE, status: "error", message: "getaddrinfo ENOTFOUND github.com", checking: true, failure: "check",
          releaseUrl: LATEST });
      updater.emit(event, payload);
      expect(stateOf(getUpdateState())).toEqual(result);
      expect(getUpdateState().log.slice(0, 2)).toEqual(
        ["Update error: getaddrinfo ENOTFOUND github.com", "Checking for update..."]);
    }
  });

test("a failed download is kept through the app's own checks until a retried download finishes",
  function test_download_error_survives_background_checks() {
    const { updater, sent, getUpdateState } = setup();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("download-progress", { percent: 30 });
    updater.emit("error", new Error("sha512 checksum mismatch"));
    const failed = getUpdateState();
    expect(stateOf(failed)).toEqual(
      { ...IDLE, status: "error", version: "2.0.1", message: "sha512 checksum mismatch", downloadFailed: true, failure: "verify" });
    sent.length = 0;
    // The periodic re-check finds nothing, then finds the update and downloads it again.
    for (const [event, payload] of [
      ["checking-for-update"], ["update-not-available", {}],
      ["checking-for-update"], ["update-available", { version: "2.0.1" }], ["download-progress", { percent: 60 }],
    ]) {
      updater.emit(event, payload);
      expect(getUpdateState()).toEqual(failed);
    }
    expect(sent).toEqual([]);
    updater.emit("update-downloaded", { version: "2.0.1" });
    expect(stateOf(getUpdateState())).toEqual(DOWNLOADED);
  });

test("the log keeps the newest 200 lines", function test_log_is_capped() {
  const { updater, getUpdateState } = setup();
  for (let i = 0; i < 250; i++) updater.emit("error", new Error("e" + i));
  const log = getUpdateState().log;
  expect(log.length).toBe(200);
  expect([log[0], log[199]]).toEqual(["Update error: e50", "Update error: e249"]);
});

// A fake ipcMain and window, as app-main.js passes them.
function serveSetup(canCheck = false) {
  const handlers = {};
  const ipcMain = { handle: (channel, fn) => { handlers[channel] = fn; } };
  const windowSent = [];
  let win = { webContents: { send: (channel, state) => windowSent.push([channel, state]) } };
  const updater = new EventEmitter();
  // electron-updater resolves a check that ran with its result.
  updater.checkForUpdates = jest.fn(() => Promise.resolve({ updateInfo: {} }));
  serveUpdateState(ipcMain, () => win, updater, canCheck);
  return { handlers, windowSent, updater, closeWindow: () => { win = null; } };
}

test("get-update-state answers idle before the updater checks (dev builds)",
  function test_serve_answers_idle_before_a_check() {
    const { handlers, windowSent } = serveSetup();
    expect(Object.keys(handlers)).toEqual(["get-update-state", "check-for-updates"]);
    expect(handlers["get-update-state"]()).toEqual({ ...IDLE, log: [], revision: 0 });
    expect(windowSent).toEqual([]);
  });

test("the updater's changes reach the window and get-update-state",
  function test_serve_forwards_to_window_and_answers_state() {
    const { handlers, windowSent, updater, closeWindow } = serveSetup();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("update-downloaded", { version: "2.0.1" });
    const replay = handlers["get-update-state"]();
    expect(stateOf(replay)).toEqual(DOWNLOADED);
    expect(windowSent.map(([ch]) => ch)).toEqual(["update-available", "update-downloaded"]);
    expect(windowSent[1][1]).toEqual(replay);
    // With no window open, changes are still kept for the next replay.
    closeWindow();
    updater.emit("error", new Error("late"));
    expect(windowSent.length).toBe(2);
    expect(handlers["get-update-state"]().log.slice(-1)).toEqual(["Update error: late"]);
  });

test("check-for-updates asks the updater to check in packaged builds only",
  function test_user_check_runs_only_when_the_app_can_check() {
    for (const canCheck of [true, false]) {
      const { handlers, updater } = serveSetup(canCheck);
      handlers["check-for-updates"]();
      expect([canCheck, updater.checkForUpdates.mock.calls.length]).toEqual([canCheck, canCheck ? 1 : 0]);
    }
  });

test("a check the user asks for dismisses a failed download, and the check's result then shows",
  function test_user_check_dismisses_a_failed_download() {
    const { handlers, updater } = serveSetup();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("error", new Error("sha512 checksum mismatch"));
    handlers["check-for-updates"]();
    expect(stateOf(handlers["get-update-state"]())).toEqual(
      { ...IDLE, status: "error", version: "2.0.1", message: "sha512 checksum mismatch", failure: "verify" });
    updater.emit("checking-for-update");
    updater.emit("update-not-available", {});
    const state = handlers["get-update-state"]();
    expect(stateOf(state)).toEqual(UP_TO_DATE);
    expect(state.log).toContain("Update error: sha512 checksum mismatch");
  });

test("every change sent carries a higher revision than the state read before it",
  function test_revision_rises_with_every_change() {
    const { handlers, windowSent, updater } = serveSetup();
    const before = handlers["get-update-state"]();
    updater.emit("checking-for-update");
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("download-progress", { percent: 50 });
    updater.emit("update-not-available", {}); // ignored: nothing sent, revision unchanged
    const revisions = windowSent.map(([, state]) => state.revision);
    expect([before.revision, ...revisions]).toEqual([0, 1, 2, 3]);
    expect(handlers["get-update-state"]().revision).toBe(3);
  });

test("a failure links the release page of the version being installed, or the latest release before one is known",
  function test_error_carries_release_page_url() {
    const { updater, getUpdateState } = setup();
    updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
    expect(getUpdateState().releaseUrl).toBe("https://github.com/celerp/celerp/releases/latest");
    updater.emit("update-available", { version: "2.6.1" });
    expect(getUpdateState().releaseUrl).toBe("");
    updater.emit("error", new Error("EPERM: operation not permitted, rename"));
    expect(getUpdateState().releaseUrl).toBe("https://github.com/celerp/celerp/releases/tag/v2.6.1");
  });

test("onFailure hears of a failure only after a check the user asked for, once per check",
  function test_on_failure_only_after_a_user_check() {
    const updater = new EventEmitter();
    const failures = [];
    const { userChecks } = trackUpdater(updater, () => {}, (state) => failures.push(state.message));
    updater.emit("error", new Error("offline"));            // the app's own check fails
    updater.emit("update-available", { version: "2.6.1" });
    updater.emit("error", new Error("EPERM: rename"));      // its download fails
    expect(failures).toEqual([]);
    userChecks();
    updater.emit("update-available", { version: "2.6.1" });
    updater.emit("error", new Error("sha512 checksum mismatch"));
    updater.emit("error", new Error("sha512 checksum mismatch"));
    expect(failures).toEqual(["sha512 checksum mismatch"]);
    userChecks();
    updater.emit("update-not-available");
    updater.emit("error", new Error("offline"));
    expect(failures).toEqual(["sha512 checksum mismatch"]);
  });

test("a failure is classed by what failed; only a download that failed its security check offers no installer",
  function test_failure_kinds() {
    const failed = (message, version = "2.6.1") => {
      const { updater, getUpdateState } = setup();
      if (version) updater.emit("update-available", { version });
      updater.emit("error", new Error(message));
      const { failure, releaseUrl } = getUpdateState();
      return [failure, releaseUrl];
    };
    const tag = "https://github.com/celerp/celerp/releases/tag/v2.6.1";
    expect(failed("EPERM: operation not permitted, rename 'a' -> 'b'")).toEqual(["save", tag]);
    expect(failed("EACCES: permission denied")).toEqual(["save", tag]);
    expect(failed("EBUSY: resource busy or locked")).toEqual(["save", tag]);
    expect(failed("sha512 checksum mismatch, expected abc, got def")).toEqual(["verify", ""]);
    expect(failed("New version 2.6.1 is not signed by the application owner: publisherNames: Celerp"))
      .toEqual(["verify", ""]);
    expect(failed("net::ERR_CONNECTION_RESET")).toEqual(["download", tag]);
    expect(failed("getaddrinfo ENOTFOUND github.com", "")).toEqual(["check", LATEST]);
  });

test("an installer that could not start moves a downloaded update to a failed install that offers the installer",
  function test_install_failed() {
    const updater = new EventEmitter();
    const sent = [];
    const { installFailed, getUpdateState } = trackUpdater(updater, (channel, s) => sent.push(channel));
    updater.emit("update-available", { version: "2.6.1" });
    updater.emit("update-downloaded", { version: "2.6.1" });
    installFailed("2.6.1", "spawn UNKNOWN", "install");
    const state = getUpdateState();
    expect(stateOf(state)).toEqual({ ...IDLE, status: "error", version: "2.6.1", message: "spawn UNKNOWN",
      downloadFailed: true, failure: "install", releaseUrl: "https://github.com/celerp/celerp/releases/tag/v2.6.1" });
    expect(state.log.slice(-1)).toEqual(["Update error: spawn UNKNOWN"]);
    expect(sent.slice(-1)).toEqual(["update-error"]);
    updater.emit("update-available", { version: "2.6.1" });
    expect(getUpdateState().failure).toBe("install");
  });

test("a setup that started and then stopped keeps its own failure kind, so the app can say what happened",
  function test_stopped_install_state() {
    const updater = new EventEmitter();
    const { installFailed, getUpdateState } = trackUpdater(updater, () => {});
    installFailed("2.8.1", "", "stopped");
    const state = getUpdateState();
    expect([state.status, state.failure, state.version]).toEqual(["error", "stopped", "2.8.1"]);
    // with no message there is no log line: the card's own text says what happened
    expect(state.log).toEqual([]);
  });

test("a version that is not a release number links the latest release, never a page it names",
  function test_release_url_needs_a_version() {
    const updater = new EventEmitter();
    const { installFailed, getUpdateState } = trackUpdater(updater, () => {});
    installFailed("2.8.1/../../../other/repo/releases/tag/v1", "", "stopped");
    expect(getUpdateState().releaseUrl).toBe(LATEST);
    installFailed("2.8.1-beta.2", "", "stopped");
    expect(getUpdateState().releaseUrl).toBe("https://github.com/celerp/celerp/releases/tag/v2.8.1-beta.2");
  });

test("nothing newer is kept as upToDate until the next result, which clears it",
  function test_up_to_date_until_the_next_result() {
    for (const [event, payload, result] of [
      ["update-available", { version: "2.0.1" }, { ...IDLE, status: "downloading", version: "2.0.1" }],
      ["error", new Error("getaddrinfo ENOTFOUND github.com"),
        { ...IDLE, status: "error", message: "getaddrinfo ENOTFOUND github.com", failure: "check", releaseUrl: LATEST }],
    ]) {
      const { updater, getUpdateState } = setup();
      updater.emit("update-not-available", {});
      updater.emit("checking-for-update");
      // While the next check runs, the last result is still there.
      expect(stateOf(getUpdateState())).toEqual({ ...UP_TO_DATE, checking: true });
      updater.emit(event, payload);
      expect(stateOf(getUpdateState())).toEqual(result);
    }
  });

test("a downloaded update handed to its installer is a change like any other: sent with a higher revision, once",
  function test_installing_is_sent_with_a_higher_revision() {
    const { updater, sent } = downloadedSetup();
    const tracked = trackUpdater(updater, (channel, payload) => sent.push([channel, payload]));
    updater.emit("update-downloaded", { version: "2.0.1" });
    sent.length = 0;
    const before = tracked.getUpdateState();
    expect(tracked.installing()).toBe(true);
    const after = tracked.getUpdateState();
    expect(stateOf(after)).toEqual({ ...DOWNLOADED, installing: true });
    expect(after.revision).toBeGreaterThan(before.revision);
    expect(sent).toEqual([["update-log", after]]);
    // A second hand-over changes nothing and sends nothing.
    expect(tracked.installing()).toBe(false);
    expect(tracked.getUpdateState()).toEqual(after);
    expect(sent.length).toBe(1);
    // An installer that did not start replaces it with the failed install.
    tracked.installFailed("2.0.1", "", "install");
    expect(tracked.getUpdateState()).toMatchObject({ status: "error", failure: "install", installing: false });
  });

test("nothing but a downloaded update can be handed to its installer", function test_installing_needs_downloaded() {
  for (const events of [[], [["update-available", { version: "2.0.1" }]], [["update-not-available", {}]]]) {
    const updater = new EventEmitter();
    const tracked = trackUpdater(updater, () => {});
    for (const [event, payload] of events) updater.emit(event, payload);
    const before = tracked.getUpdateState();
    expect(tracked.installing()).toBe(false);
    expect(tracked.getUpdateState()).toEqual(before);
  }
});

test("a check the user asks for that runs nothing ends as a failed check, never checking forever",
  async function test_user_check_that_runs_nothing_fails() {
    for (const nothing of [null, undefined]) {
      const { handlers, updater, windowSent } = serveSetup(true);
      updater.checkForUpdates = jest.fn(() => Promise.resolve(nothing));
      updater.emit("update-not-available", {});
      await handlers["check-for-updates"]();
      await new Promise(setImmediate);
      const state = handlers["get-update-state"]();
      expect(stateOf(state)).toEqual({ ...IDLE, status: "error", failure: "inactive", releaseUrl: LATEST });
      expect(state.log).toEqual(["This copy of Celerp cannot update itself."]);
      expect(windowSent.at(-1)).toEqual(["update-error", state]);
    }
    // A check that ran leaves the state to the updater's events.
    const { handlers } = serveSetup(true);
    await handlers["check-for-updates"]();
    await new Promise(setImmediate);
    expect(stateOf(handlers["get-update-state"]())).toEqual(IDLE);
  });

test("a check that ran nothing is not the user's any more: a later failure of the app's own check is not told",
  async function test_check_that_ran_nothing_clears_user_asked() {
    const ipcMain = { handle: (channel, fn) => { ipcMain[channel] = fn; } };
    const updater = new EventEmitter();
    updater.checkForUpdates = () => Promise.resolve(null);
    const told = [];
    serveUpdateState(ipcMain, () => null, updater, true, (state) => told.push(state.failure));
    await ipcMain["check-for-updates"]();
    await new Promise(setImmediate);
    updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
    expect(told).toEqual([]);
  });

test("a check that rejects with no error event ends as a failed check, never checking forever",
  async function test_rejected_check_without_error_event_fails() {
    const { handlers, updater, windowSent } = serveSetup(true);
    updater.checkForUpdates = jest.fn(() => {
      updater.emit("checking-for-update");
      return Promise.reject(new Error("net::ERR_INTERNET_DISCONNECTED"));
    });
    await handlers["check-for-updates"]();
    await new Promise(setImmediate);
    const state = handlers["get-update-state"]();
    expect(stateOf(state)).toEqual({ ...IDLE, status: "error", failure: "check", releaseUrl: LATEST,
                                     message: "net::ERR_INTERNET_DISCONNECTED" });
    expect(state.log).toEqual(["Checking for update...", "Update error: net::ERR_INTERNET_DISCONNECTED"]);
    expect(windowSent.at(-1)).toEqual(["update-error", state]);
  });

test("a check that rejects after its error event is logged once, and told to the user once",
  async function test_rejected_check_with_error_event_logs_once() {
    const ipcMain = { handle: (channel, fn) => { ipcMain[channel] = fn; } };
    const updater = new EventEmitter();
    updater.checkForUpdates = () => {
      const err = new Error("getaddrinfo ENOTFOUND github.com");
      updater.emit("error", err);
      return Promise.reject(err);
    };
    const told = [];
    serveUpdateState(ipcMain, () => null, updater, true, (state) => told.push(state.failure));
    await ipcMain["check-for-updates"]();
    await new Promise(setImmediate);
    expect(ipcMain["get-update-state"]().log).toEqual(["Update error: getaddrinfo ENOTFOUND github.com"]);
    expect(told).toEqual(["check"]);
  });

test("a check that throws before it returns a promise fails the same way",
  async function test_check_that_throws_fails() {
    const { handlers, updater } = serveSetup(true);
    updater.checkForUpdates = () => { throw new Error("no feed"); };
    await handlers["check-for-updates"]();
    await new Promise(setImmediate);
    expect(handlers["get-update-state"]()).toMatchObject({ status: "error", failure: "check", message: "no feed" });
  });

test("the app's own checks run through the same check: a rejection with no error event shows on the update card",
  async function test_app_check_rejection_fails() {
    const updater = new EventEmitter();
    updater.checkForUpdates = () => Promise.reject(new Error("timeout"));
    const told = [];
    const served = serveUpdateState({ handle: () => {} }, () => null, updater, true, (state) => told.push(state));
    await served.check();
    expect(served.getUpdateState()).toMatchObject({ status: "error", failure: "check", message: "timeout" });
    expect(told).toEqual([]);   // the app's own check fails on the update card only
  });

test("every update check and download in the app goes through update-state.js, which records the updater's first use",
  function test_one_check_census() {
    const fs = require("fs");
    const path = require("path");
    const sources = fs.readdirSync(path.join(__dirname, "..")).filter((f) => f.endsWith(".js"));
    const calls = (pattern) => sources.flatMap((f) =>
      (fs.readFileSync(path.join(__dirname, "..", f), "utf8").match(pattern) || []).map(() => f));
    expect(calls(/\.checkForUpdates\(/g)).toEqual(["update-state.js"]);
    expect(calls(/\.downloadUpdate\(/g)).toEqual(["update-state.js"]);
  });

// A fake electron-updater that checks as the real one does: it tells the release it found, then
// downloads it itself only while autoDownload is on. Its own download is a second downloadUpdate call.
function releaseFeed(latest) {
  const updater = new EventEmitter();
  updater.autoDownload = true;
  updater.downloadUpdate = jest.fn(() => Promise.resolve([]));
  updater.checkForUpdates = jest.fn(async () => {
    updater.emit("checking-for-update");
    updater.emit("update-available", { version: latest });
    if (updater.autoDownload) updater.downloadUpdate();
    return { updateInfo: { version: latest } };
  });
  const told = [];
  const served = serveUpdateState({ handle: () => {} }, () => null, updater, true, (state) => told.push(state));
  return { updater, told, served };
}

test("for data a newer Celerp opened, a release that cannot open it is never downloaded, and nothing about it is told",
  async function test_too_old_release_is_not_downloaded() {
    const { updater, told, served } = releaseFeed("2.9.0");
    served.downloadOnlyFrom("2.9.1");
    await served.checkForUser();
    expect(updater.downloadUpdate).not.toHaveBeenCalled();
    expect(stateOf(served.getUpdateState())).toEqual({ ...IDLE, status: "too-old", version: "2.9.0" });
    expect(served.getUpdateState().log.at(-1)).toBe("Found v2.9.0, which cannot open this data.");
    // An updater error after it (nothing is downloading) is logged, and changes nothing.
    updater.emit("error", new Error("EPERM: operation not permitted, rename 'x.exe'"));
    expect(told).toEqual([]);
    expect(stateOf(served.getUpdateState())).toEqual({ ...IDLE, status: "too-old", version: "2.9.0" });
    expect(served.getUpdateState().log.at(-1)).toMatch(/^Update error: EPERM/);
    // The app's own downloads are left as they were.
    expect(updater.autoDownload).toBe(true);
  });

test("for data a newer Celerp opened, a release that opens it downloads once and installs as before",
  async function test_release_that_opens_the_data_downloads() {
    for (const latest of ["2.9.1", "2.10.0"]) {
      const { updater, served } = releaseFeed(latest);
      served.downloadOnlyFrom("2.9.1");
      await served.checkForUser();
      expect([latest, updater.downloadUpdate.mock.calls.length]).toEqual([latest, 1]);
      expect(updater.autoDownload).toBe(true);
      updater.emit("update-downloaded", { version: latest });
      expect(served.getUpdateState()).toMatchObject({ status: "downloaded", version: latest });
      expect(served.installing()).toBe(true);
    }
  });

test("a download that fails after a release that opens the data was found is told, with that release's installer",
  async function test_release_that_opens_the_data_failure_is_told() {
    const { updater, told, served } = releaseFeed("2.9.1");
    updater.downloadUpdate = jest.fn(() => {
      const err = new Error("EPERM: operation not permitted, rename 'x.exe'");
      updater.emit("error", err);
      return Promise.reject(err);
    });
    served.downloadOnlyFrom("2.9.1");
    await served.checkForUser();
    await Promise.resolve();
    expect(told.map((s) => [s.failure, s.releaseUrl]))
      .toEqual([["save", "https://github.com/celerp/celerp/releases/tag/v2.9.1"]]);
  });

test("with no version on record (an unknown schema) any release found downloads",
  async function test_unknown_schema_downloads_any_release() {
    const { updater, served } = releaseFeed("2.5.4");
    served.downloadOnlyFrom("");
    await served.checkForUser();
    expect(updater.downloadUpdate).toHaveBeenCalledTimes(1);
    expect(served.getUpdateState()).toMatchObject({ status: "downloading", version: "2.5.4" });
  });

test("the running app's own checks and the user's checks still download by themselves",
  async function test_running_app_checks_auto_download() {
    const { updater, served } = releaseFeed("2.9.0");
    await served.check();
    await served.checkForUser();
    expect(updater.autoDownload).toBe(true);
    expect(updater.downloadUpdate).toHaveBeenCalledTimes(2);
    expect(served.getUpdateState()).toMatchObject({ status: "downloading", version: "2.9.0" });
  });

test("release numbers compare by their numbers, and a prerelease is older than its release",
  function test_compare_versions() {
    for (const [a, b, sign] of [
      ["2.6.0", "2.6.0", 0], ["2.6.1", "2.6.0", 1], ["2.5.9", "2.6.0", -1], ["2.10.0", "2.9.9", 1],
      ["3.0.0", "2.99.99", 1], ["2.6.0-beta.1", "2.6.0", -1], ["2.6.0", "2.6.0b1", 1],
      ["2.6.0-beta.2", "2.6.0-beta.10", -1], ["2.6.0-beta.1", "2.6.0-beta.1", 0],
    ]) {
      expect([a, b, Math.sign(compareVersions(a, b))]).toEqual([a, b, sign]);
    }
    for (const [a, b] of [["", "2.6.0"], ["2.6", "2.6.0"], ["latest", "2.6.0"], ["2.6.0", undefined]]) {
      expect(compareVersions(a, b)).toBeNaN();
    }
  });

test("a release that is older than the data, or not a release number, cannot open it",
  async function test_unreadable_or_older_release_is_too_old() {
    for (const latest of ["2.9.0", "2.9.1-beta.1", "not-a-version"]) {
      const { updater, served } = releaseFeed(latest);
      served.downloadOnlyFrom("2.9.1");
      await served.checkForUser();
      expect([latest, updater.downloadUpdate.mock.calls.length, served.getUpdateState().status])
        .toEqual([latest, 0, "too-old"]);
    }
  });

// The running app's own check found `latest` (autoDownload on, no rule yet).
async function runningAppFound(latest) {
  const feed = releaseFeed(latest);
  await feed.served.check();
  return feed;
}

// What a refusal shows when the updater was used before the version rule was set: only a restart,
// whose new process sets the rule before its first check.
const RELAUNCH = { ...IDLE, status: "relaunch" };

// For each state the running app's updater can leave behind (2.9.0, which cannot open data 2.9.1
// opened, downloaded, still downloading, or failed to download), with 2.9.1 now in the feed.
const BEFORE_THE_RULE = {
  downloaded: (updater) => updater.emit("update-downloaded", { version: "2.9.0" }),
  downloading: (updater) => updater.emit("download-progress", { percent: 40 }),
  failed: (updater) => updater.emit("error", new Error("net::ERR_CONNECTION_RESET")),
};

test.each(Object.keys(BEFORE_THE_RULE))(
  "updater state from before the refusal (%s) never stands for a release: the screen offers a restart, and nothing more runs",
  async function test_used_before_rule_offers_relaunch(left) {
    const { updater, told, served } = await runningAppFound("2.9.0");
    BEFORE_THE_RULE[left](updater);
    updater.checkForUpdates.mockClear();
    updater.downloadUpdate.mockClear();
    served.downloadOnlyFrom("2.9.1");
    expect(stateOf(served.getUpdateState())).toEqual(RELAUNCH);
    // No check runs in this process any more, the app's own or the user's, and nothing installs.
    await served.check();
    await served.checkForUser();
    expect(updater.checkForUpdates).not.toHaveBeenCalled();
    expect(updater.downloadUpdate).not.toHaveBeenCalled();
    expect(served.installing()).toBe(false);
    // A check or download already in flight that finds, ends or fails changes nothing on the screen,
    // downloads nothing and tells nothing.
    updater.emit("update-available", { version: "2.9.1" });
    expect(updater.downloadUpdate).not.toHaveBeenCalled();
    updater.emit("error", new Error("net::ERR_CONNECTION_RESET"));
    updater.emit("update-downloaded", { version: "2.9.0" });
    expect(stateOf(served.getUpdateState())).toEqual(RELAUNCH);
    expect(served.installing()).toBe(false);
    expect(told).toEqual([]);
    // The relaunched process sets the rule before its first check: it finds 2.9.1 and installs it.
    const relaunched = releaseFeed("2.9.1");
    relaunched.served.downloadOnlyFrom("2.9.1");
    await relaunched.served.checkForUser();
    expect(relaunched.updater.downloadUpdate).toHaveBeenCalledTimes(1);
    relaunched.updater.emit("update-downloaded", { version: "2.9.1" });
    expect(relaunched.served.installing()).toBe(true);
  });

test("a release that opens the data, downloaded before the refusal, is not installed from that process either",
  async function test_downloaded_before_rule_that_opens_the_data_relaunches() {
    const { updater, served } = await runningAppFound("2.9.1");
    updater.emit("update-downloaded", { version: "2.9.1" });
    served.downloadOnlyFrom("2.9.1");
    expect(stateOf(served.getUpdateState())).toEqual(RELAUNCH);
    expect(served.installing()).toBe(false);
  });

test("a check that has not answered yet is use of the updater: the rule set after it offers a restart",
  function test_check_in_flight_counts_as_use() {
    const { updater, served } = releaseFeed("2.9.1");
    updater.checkForUpdates = jest.fn(() => new Promise(() => {}));
    served.check();
    served.downloadOnlyFrom("2.9.1");
    expect(stateOf(served.getUpdateState())).toEqual(RELAUNCH);
  });

test("the rule set before any check is a clean start: a release that opens the data is offered, one that cannot is too-old",
  async function test_rule_first_is_clean() {
    for (const [latest, status] of [["2.9.1", "downloading"], ["2.9.0", "too-old"]]) {
      const { updater, served } = releaseFeed(latest);
      served.downloadOnlyFrom("2.9.1");
      await served.checkForUser();
      expect([latest, served.getUpdateState().status, updater.checkForUpdates.mock.calls.length])
        .toEqual([latest, status, 1]);
    }
  });

test("a setup that stopped for a release that cannot open the data offers a restart, with no failure to tell",
  function test_stopped_install_of_too_old_release() {
    const { updater, served } = releaseFeed("2.9.0");
    served.installFailed("2.9.0", "", "stopped");
    served.downloadOnlyFrom("2.9.1");
    expect(stateOf(served.getUpdateState())).toEqual(RELAUNCH);
    served.checkForUser();
    expect(updater.checkForUpdates).not.toHaveBeenCalled();
  });

test("a setup that stopped for a release that opens the data keeps its failure, and Update Celerp checks again",
  async function test_stopped_install_of_release_that_opens_the_data() {
    const { updater, served } = releaseFeed("2.9.1");
    served.installFailed("2.9.1", "", "stopped");
    served.downloadOnlyFrom("2.9.1");
    expect(served.getUpdateState()).toMatchObject({ status: "error", failure: "stopped", version: "2.9.1" });
    await served.checkForUser();
    expect(updater.downloadUpdate).toHaveBeenCalledTimes(1);
    expect(served.getUpdateState()).toMatchObject({ status: "downloading", version: "2.9.1" });
  });

test("with no version on record (an unknown schema), updater state from before the refusal stands as it was",
  async function test_no_rule_after_use_is_unchanged() {
    const { updater, served } = await runningAppFound("2.9.0");
    updater.emit("update-downloaded", { version: "2.9.0" });
    served.downloadOnlyFrom("");
    expect(served.getUpdateState()).toMatchObject({ status: "downloaded", version: "2.9.0" });
    expect(served.installing()).toBe(true);
  });

test("with no rule, a downloaded release is offered and installs whatever its version",
  async function test_no_rule_downloaded_installs() {
    const { updater, served } = await runningAppFound("2.0.1");
    updater.emit("update-downloaded", { version: "2.0.1" });
    expect(stateOf(served.getUpdateState())).toEqual(DOWNLOADED);
    expect(served.installing()).toBe(true);
  });

test("release numbers are compared only in update-state.js: every other part of the app reads its judged state",
  function test_one_version_judge_census() {
    const fs = require("fs");
    const path = require("path");
    const root = path.join(__dirname, "..");
    const sources = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
      const full = path.join(dir, e.name);
      if (e.isDirectory()) return ["node_modules", "tests", "dist"].includes(e.name) ? [] : sources(full);
      return e.name.endsWith(".js") ? [full] : [];
    });
    const users = sources(root).filter((f) => /\b(compareVersions|opensData)\b/.test(fs.readFileSync(f, "utf8")))
      .map((f) => path.relative(root, f));
    expect(users).toEqual(["update-state.js"]);
  });

test("for data a newer Celerp opened, a release found with no version cannot open it and is not downloaded",
  async function test_release_with_no_version_is_too_old() {
    const { updater, served } = releaseFeed(undefined);
    served.downloadOnlyFrom("2.9.1");
    await served.checkForUser();
    expect(updater.downloadUpdate).not.toHaveBeenCalled();
    expect(served.getUpdateState()).toMatchObject({ status: "too-old", version: "" });
  });

test("after the user's check found a release that cannot open the data, a later download failure of the app's own check is not told",
  async function test_too_old_result_ends_the_users_check() {
    const { updater, told, served } = releaseFeed("2.9.0");
    served.downloadOnlyFrom("2.9.1");
    await served.checkForUser();
    updater.checkForUpdates = async () => { updater.emit("update-available", { version: "2.9.2" }); return { updateInfo: {} }; };
    updater.downloadUpdate = jest.fn(() => { updater.emit("error", new Error("net::ERR_CONNECTION_RESET")); return Promise.resolve([]); });
    await served.check();
    expect(updater.downloadUpdate).toHaveBeenCalledTimes(1);
    expect(served.getUpdateState()).toMatchObject({ status: "error", failure: "download", version: "2.9.2" });
    expect(told).toEqual([]);
  });
