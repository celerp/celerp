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
});
