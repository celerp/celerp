// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Preload: exposes a minimal, safe bridge to the renderer.
// No Node APIs are exposed directly — contextIsolation is on.

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("celerp", {
  // Renderer can call this to open a URL in the system browser
  openExternal: (url) => ipcRenderer.invoke("open-external", url),

  // Renderer calls this for hx-confirm dialogs; returns true if user clicked OK.
  // Synchronous round-trip via ipcRenderer.sendSync so the htmx confirm handler
  // can return a plain boolean without needing async/await.
  showConfirm: (message) => ipcRenderer.sendSync("show-confirm", message),

  // Returns the current app version string.
  getVersion: () => ipcRenderer.invoke("get-version"),

  // Modules page: open the modules folder in the OS file manager.
  openModulesFolder: () => ipcRenderer.invoke("open-modules-folder"),

  // Modules page: native folder picker; resolves to a path string or null.
  pickModuleFolder: () => ipcRenderer.invoke("pick-module-folder"),

  // Updater state ({ status, version, percent, message, checking, log }), read-only.
  // Every page load replays it; the update callbacks below receive the same shape.
  getUpdateState: () => ipcRenderer.invoke("get-update-state"),

  // Register a callback for when an update is found (state: downloading).
  onUpdateAvailable: (cb) => ipcRenderer.on("update-available", (_event, state) => cb(state)),

  // Register a callback for when an update has been downloaded and is ready to install.
  onUpdateDownloaded: (cb) => ipcRenderer.on("update-downloaded", (_event, state) => cb(state)),

  // Register a callback for when a check finds no update (state: idle).
  onUpdateNotAvailable: (cb) => ipcRenderer.on("update-not-available", (_event, state) => cb(state)),

  // Register a callback for download progress (state: downloading, with percent).
  onDownloadProgress: (cb) => ipcRenderer.on("download-progress", (_event, state) => cb(state)),

  // Register a callback for a new updater log line (the state, with its log).
  onUpdateLog: (cb) => ipcRenderer.on("update-log", (_event, state) => cb(state)),

  // Trigger a manual update check.
  checkForUpdates: () => ipcRenderer.invoke("check-for-updates"),

  // Register a callback for update errors (state: error, with message). Once
  // an update is downloaded the state stays downloaded; the error is only logged.
  onUpdateError: (cb) => ipcRenderer.on("update-error", (_event, state) => cb(state)),

  // Uninstall: quit and keep data (shows instructions to remove .app manually)
  uninstallKeepData: () => ipcRenderer.invoke("uninstall-keep-data"),

  // Uninstall: delete all user data then quit (irreversible)
  uninstallDeleteData: () => ipcRenderer.invoke("uninstall-delete-data"),

  // Quit and install the downloaded update immediately.
  installUpdate: () => ipcRenderer.send("install-update"),

  // Full app relaunch to apply a saved infrastructure change (DB/storage mode).
  restartApp: () => ipcRenderer.send("restart-app"),
});
