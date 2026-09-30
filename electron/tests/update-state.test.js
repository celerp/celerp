// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const { EventEmitter } = require("events");
const { trackUpdater } = require("../update-state");

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

const DOWNLOADED = { status: "downloaded", version: "2.0.1", percent: 100, message: "" };

test("a fresh tracker starts idle", function test_new_tracker_starts_idle() {
  const { getUpdateState } = setup();
  expect(getUpdateState()).toEqual({ status: "idle", version: "", percent: 0, message: "" });
});

test("relaunch starts fresh: a new tracker is idle even after another reached downloaded",
  function test_second_tracker_starts_idle_after_downloaded() {
    const first = downloadedSetup();
    expect(first.getUpdateState().status).toBe("downloaded");
    const second = setup();
    expect(second.getUpdateState()).toEqual({ status: "idle", version: "", percent: 0, message: "" });
  });

test("update found goes straight to downloading and is forwarded", function test_update_found_starts_downloading() {
  const { updater, sent, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  const state = { status: "downloading", version: "2.0.1", percent: 0, message: "" };
  expect(getUpdateState()).toEqual(state);
  expect(sent).toEqual([["update-available", state]]);
});

test("progress updates the percent and keeps the version", function test_progress_updates_percent() {
  const { updater, getUpdateState } = setup();
  updater.emit("update-available", { version: "2.0.1" });
  updater.emit("download-progress", { percent: 12.5 });
  expect(getUpdateState()).toEqual({ status: "downloading", version: "2.0.1", percent: 12.5, message: "" });
  updater.emit("download-progress", { percent: 80 });
  expect(getUpdateState().percent).toBe(80);
});

test("state carries version, percent and error message", function test_state_carries_version_percent_message() {
  const { updater, getUpdateState } = setup();
  updater.emit("update-available", { version: "3.1.0" });
  updater.emit("download-progress", { percent: 40 });
  expect(getUpdateState()).toMatchObject({ version: "3.1.0", percent: 40 });
  updater.emit("error", new Error("disk full"));
  expect(getUpdateState()).toMatchObject({ status: "error", version: "3.1.0", message: "disk full" });
  const done = downloadedSetup();
  expect(done.getUpdateState()).toEqual(DOWNLOADED);
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
    expect(getUpdateState()).toEqual(DOWNLOADED);
    expect(sent).toEqual([]);
  }
});

test("an error before any download is retained for later replay", function test_pre_download_error_is_retained() {
  const { updater, sent, getUpdateState } = setup();
  updater.emit("error", new Error("getaddrinfo ENOTFOUND github.com"));
  const state = { status: "error", version: "", percent: 0, message: "getaddrinfo ENOTFOUND github.com" };
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
    expect(getUpdateState().status).toBe("downloading");
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
