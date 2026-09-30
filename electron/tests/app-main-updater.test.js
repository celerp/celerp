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
    fs.readFileSync(APP_MAIN, "utf8"));
  body(mod.exports, fakeRequire, mod, APP_MAIN, path.dirname(APP_MAIN));
  return { handlers, updater };
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
