// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Loads the real app-main.js with a fake electron and electron-updater, so the
// updater wiring it does at load time is exercised, not just update-state.js.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { createRequire } = require("module");
const { EventEmitter } = require("events");

const APP_MAIN = path.join(__dirname, "..", "app-main.js");

// app-main.js keeps its window in a module variable that createWindow sets once
// the app is ready. The app never becomes ready here, so the test sets it.
const SET_MAIN_WINDOW = "\n;module.exports.setMainWindow = (win) => { mainWindow = win; };\n";

// Runs app-main.js as Node would (it has a top-level return), resolving the
// modules below to fakes and everything else normally. The app never becomes
// ready, so only the load-time wiring runs.
function loadAppMain() {
  const handlers = {};
  const updater = new EventEmitter();
  const fakes = {
    electron: {
      app: {
        isPackaged: false,
        getPath: () => os.tmpdir(),
        getAppPath: () => path.dirname(APP_MAIN),
        getVersion: () => "0.0.0",
        requestSingleInstanceLock: () => true,
        whenReady: () => new Promise(() => {}),
        on: () => {},
        quit: () => {},
      },
      ipcMain: {
        handle: (channel, fn) => { handlers[channel] = fn; },
        on: () => {},
      },
      BrowserWindow: function BrowserWindow() {},
      shell: {},
      dialog: {},
      Menu: {},
      powerSaveBlocker: {},
    },
    "electron-updater": { autoUpdater: updater },
    "async-exit-hook": function add() {},
  };
  const realRequire = createRequire(APP_MAIN);
  const cache = { "async-exit-hook": { exports: fakes["async-exit-hook"] } };
  const fakeRequire = (id) => (id in fakes ? fakes[id] : realRequire(id));
  fakeRequire.resolve = (id) => (id in fakes ? id : realRequire.resolve(id));
  fakeRequire.cache = cache;
  const mod = { exports: {} };
  const body = new Function("exports", "require", "module", "__filename", "__dirname",
    fs.readFileSync(APP_MAIN, "utf8") + SET_MAIN_WINDOW);
  body(mod.exports, fakeRequire, mod, APP_MAIN, path.dirname(APP_MAIN));
  return { handlers, updater, setMainWindow: mod.exports.setMainWindow };
}

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
