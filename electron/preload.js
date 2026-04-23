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
});
