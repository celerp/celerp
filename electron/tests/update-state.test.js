// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const { EventEmitter } = require("events");
const { trackUpdater, serveUpdateState } = require("../update-state");

// A fake electron-updater: the tracker only subscribes to its events.
function setup() {
  const updater = new EventEmitter();
  const sent = [];
  const getUpdateState = trackUpdater(updater, (channel, payload) => sent.push([channel, payload]));
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

// The state without its log lines.
const stateOf = ({ log, ...state }) => state;

const IDLE = { status: "idle", version: "", percent: 0, message: "" };
const DOWNLOADED = { status: "downloaded", version: "2.0.1", percent: 100, message: "" };

test("a fresh tracker starts idle", function test_new_tracker_starts_idle() {
  const { getUpdateState } = setup();
  expect(getUpdateState()).toEqual({ ...IDLE, log: [] });
});

test("relaunch starts fresh: a new tracker is idle even after another reached downloaded",
  function test_second_tracker_starts_idle_after_downloaded() {
    const first = downloadedSetup();
    expect(first.getUpdateState().status).toBe("downloaded");
    const second = setup();
    expect(second.getUpdateState()).toEqual({ ...IDLE, log: [] });
  });

test("update found goes straight to downloading and is forwarded", function test_update_found_starts_downloading() {
  const { updater, sent, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  const state = { status: "downloading", version: "2.0.1", percent: 0, message: "" };
  expect(stateOf(getUpdateState())).toEqual(state);
  expect(sent.map(([ch, s]) => [ch, stateOf(s)])).toEqual([["update-available", state]]);
});

test("progress updates the percent and keeps the version", function test_progress_updates_percent() {
  const { updater, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  updater.emit("download-progress", { percent: 12.5 });
  expect(stateOf(getUpdateState())).toEqual({ status: "downloading", version: "2.0.1", percent: 12.5, message: "" });
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
  const state = { status: "error", version: "", percent: 0, message: "getaddrinfo ENOTFOUND github.com",
                  log: ["Update error: getaddrinfo ENOTFOUND github.com"] };
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
      expect(getUpdateState()).toEqual({ status: "error", version: "", percent: 0, message: "", log: ["Update error"] });
    }
  });

test("a check that finds nothing does not stop a download in progress",
  function test_not_available_is_ignored_while_downloading() {
    const { updater, sent, getUpdateState } = setup();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("download-progress", { percent: 30 });
    sent.length = 0;
    updater.emit("update-not-available", {});
    expect(stateOf(getUpdateState())).toEqual({ status: "downloading", version: "2.0.1", percent: 30, message: "" });
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
      "v2.0.1 ready. Click 'Restart to Install'",
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

test("each check starts a fresh log", function test_check_starts_fresh_log() {
  const { updater, sent, getUpdateState } = setup();
  updater.emit("error", new Error("boom"));
  updater.emit("checking-for-update");
  expect(getUpdateState().log).toEqual(["Checking for update..."]);
  expect(sent[sent.length - 1]).toEqual(["update-log", getUpdateState()]);
});

test("the log keeps the newest 200 lines", function test_log_is_capped() {
  const { updater, getUpdateState } = setup();
  for (let i = 0; i < 250; i++) updater.emit("error", new Error("e" + i));
  const log = getUpdateState().log;
  expect(log.length).toBe(200);
  expect([log[0], log[199]]).toEqual(["Update error: e50", "Update error: e249"]);
});

// A fake ipcMain and window, as app-main.js passes them.
function serveSetup() {
  const handlers = {};
  const ipcMain = { handle: (channel, fn) => { handlers[channel] = fn; } };
  const windowSent = [];
  let win = { webContents: { send: (channel, state) => windowSent.push([channel, state]) } };
  const updater = new EventEmitter();
  serveUpdateState(ipcMain, () => win, updater);
  return { handlers, windowSent, updater, closeWindow: () => { win = null; } };
}

test("get-update-state answers idle before the updater checks (dev builds)",
  function test_serve_answers_idle_before_a_check() {
    const { handlers, windowSent } = serveSetup();
    expect(Object.keys(handlers)).toEqual(["get-update-state"]);
    expect(handlers["get-update-state"]()).toEqual({ ...IDLE, log: [] });
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
