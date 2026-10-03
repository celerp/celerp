// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Exercises the updater wiring app-main.js does at load time, not just
// update-state.js.

"use strict";

const { loadAppMain } = require("./app-main-loader.lib");

test("app-main serves get-update-state from the updater it tracks",
  function test_app_main_serves_the_tracked_updater_state() {
    const { handlers, updater } = loadAppMain();
    expect(typeof handlers["get-update-state"]).toBe("function");
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("update-downloaded", { version: "2.0.1" });
    const state = handlers["get-update-state"]();
    expect([state.status, state.version]).toEqual(["downloaded", "2.0.1"]);
    expect(state.log).toEqual(["Found v2.0.1, downloading...", "v2.0.1 ready. Click 'Restart to Install'"]);
  });

test("app-main's check-for-updates dismisses a failed download",
  function test_app_main_check_dismisses_a_failed_download() {
    const { handlers, updater } = loadAppMain();
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("error", new Error("sha512 checksum mismatch"));
    expect(handlers["get-update-state"]().downloadFailed).toBe(true);
    handlers["check-for-updates"]();
    expect(handlers["get-update-state"]().downloadFailed).toBe(false);
  });

test("app-main sends the updater's changes to its window as they happen",
  function test_app_main_forwards_updater_events_to_the_window() {
    const { updater, setMainWindow } = loadAppMain();
    const sent = [];
    setMainWindow({ webContents: { send: (channel, state) => sent.push([channel, state.status]) } });
    updater.emit("update-available", { version: "2.0.1" });
    updater.emit("update-downloaded", { version: "2.0.1" });
    expect(sent).toEqual([["update-available", "downloading"], ["update-downloaded", "downloaded"]]);
  });
