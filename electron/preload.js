// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BSL-1.1
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

  // Register a callback for when an update is available.
  onUpdateAvailable: (cb) => ipcRenderer.on("update-available", (_event, info) => cb(info)),

  // Register a callback for when an update has been downloaded and is ready to install.
  onUpdateDownloaded: (cb) => ipcRenderer.on("update-downloaded", (_event, info) => cb(info)),

  // Register a callback for when no update is available (or an error occurred).
  onUpdateNotAvailable: (cb) => ipcRenderer.on("update-not-available", () => cb()),

  // Register a callback for download progress events ({ percent, bytesPerSecond, transferred, total }).
  onDownloadProgress: (cb) => ipcRenderer.on("download-progress", (_event, progress) => cb(progress)),

  // Register a callback for updater log lines (string).
  onUpdateLog: (cb) => ipcRenderer.on("update-log", (_event, msg) => cb(msg)),

  // Trigger a manual update check.
  checkForUpdates: () => ipcRenderer.invoke("check-for-updates"),

  // Quit and install the downloaded update immediately.
  installUpdate: () => ipcRenderer.send("install-update"),
});
