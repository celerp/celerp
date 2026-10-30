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

// app-main.js keeps its window, servers and database in module variables that are
// set once the app is ready. Tests that never make the app ready set them themselves.
const SETTERS = "\n;module.exports.setMainWindow = (win) => { mainWindow = win; };\n" +
  ";module.exports.setServices = (api, ui, pg) => { apiProcess = api; uiProcess = ui; pgInstance = pg; };\n" +
  ";module.exports.postgresLauncher = () => postgresLauncher();\n" +
  ";module.exports.setupAutoUpdater = () => setupAutoUpdater();\n" +
  ";module.exports.windowsPrivateRoot = () => windowsPrivateRoot();\n" +
  ";module.exports.serverCommand = (asgiApp, port) => serverCommand(asgiApp, port);\n" +
  ";module.exports.publishApiPort = () => publishApiPort();\n" +
  ";module.exports.withdrawApiPort = () => withdrawApiPort();\n" +
  ";module.exports.startServer = (which, dbUrl, cfg) => { apiPort = apiPort || 9; uiPort = uiPort || 9; " +
  "return (which === \"api\" ? startApi : startUi)(dbUrl, cfg); };\n";

// Runs app-main.js as Node would (it has a top-level return), resolving the
// modules below to fakes and everything else normally. `app` and `extraFakes`
// override the defaults; `resourcesPath` stands in for the packaged app's resources
// folder; `argv` its command line; `platform` the operating system it runs on; `BrowserWindow` the window
// class. By default the app never becomes ready, so only the load-time wiring runs.
function loadAppMain({ app = {}, dialog = {}, shell = {}, extraFakes = {}, resourcesPath, argv = process.argv,
                      platform = process.platform, BrowserWindow = function BrowserWindow() {} } = {}) {
  const handlers = {};
  const listeners = {};
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
        on: (channel, fn) => { listeners[channel] = fn; },
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
  const proc = Object.create(process, { resourcesPath: { value: resourcesPath }, platform: { value: platform },
                                       argv: { value: argv } });
  const body = new Function("exports", "require", "module", "__filename", "__dirname", "process",
    fs.readFileSync(APP_MAIN, "utf8") + SETTERS);
  body(mod.exports, fakeRequire, mod, APP_MAIN, path.dirname(APP_MAIN), proc);
  return { handlers, listeners, updater, setMainWindow: mod.exports.setMainWindow,
           setServices: mod.exports.setServices, postgresLauncher: mod.exports.postgresLauncher,
           windowsPrivateRoot: mod.exports.windowsPrivateRoot, setupAutoUpdater: mod.exports.setupAutoUpdater,
           serverCommand: mod.exports.serverCommand, startServer: mod.exports.startServer,
           publishApiPort: mod.exports.publishApiPort, withdrawApiPort: mod.exports.withdrawApiPort };
}

module.exports = { loadAppMain };
