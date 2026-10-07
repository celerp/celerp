// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Loads the real app-main.js with a fake electron and electron-updater, so its
// load-time wiring and startup sequence can be exercised without Electron.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { createRequire } = require("module");
const { EventEmitter } = require("events");

const APP_MAIN = path.join(__dirname, "..", "app-main.js");

// app-main.js keeps its window in a module variable that createWindow sets once
// the app is ready. Tests that never make the app ready set it themselves.
const SET_MAIN_WINDOW = "\n;module.exports.setMainWindow = (win) => { mainWindow = win; };\n";

// Runs app-main.js as Node would (it has a top-level return), resolving the
// modules below to fakes and everything else normally. `app` and `extraFakes`
// override the defaults; `resourcesPath` stands in for the packaged app's resources
// folder; `BrowserWindow` the window class. By default the app never becomes ready, so only the load-time wiring runs.
function loadAppMain({ app = {}, dialog = {}, shell = {}, extraFakes = {}, resourcesPath,
                      BrowserWindow = function BrowserWindow() {} } = {}) {
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
        ...app,
      },
      ipcMain: {
        handle: (channel, fn) => { handlers[channel] = fn; },
        on: () => {},
      },
      BrowserWindow,
      shell,
      dialog,
      Menu: { buildFromTemplate: () => ({}), setApplicationMenu: () => {} },
      powerSaveBlocker: {},
    },
    "electron-updater": { autoUpdater: updater },
    "async-exit-hook": function add() {},
    ...extraFakes,
  };
  const realRequire = createRequire(APP_MAIN);
  const cache = { "async-exit-hook": { exports: fakes["async-exit-hook"] } };
  const fakeRequire = (id) => (id in fakes ? fakes[id] : realRequire(id));
  fakeRequire.resolve = (id) => (id in fakes ? id : realRequire.resolve(id));
  fakeRequire.cache = cache;
  const mod = { exports: {} };
  const proc = Object.create(process, { resourcesPath: { value: resourcesPath } });
  const body = new Function("exports", "require", "module", "__filename", "__dirname", "process",
    fs.readFileSync(APP_MAIN, "utf8") + SET_MAIN_WINDOW);
  body(mod.exports, fakeRequire, mod, APP_MAIN, path.dirname(APP_MAIN), proc);
  return { handlers, updater, setMainWindow: mod.exports.setMainWindow };
}

module.exports = { loadAppMain };
