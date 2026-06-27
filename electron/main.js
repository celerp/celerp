// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Ultra-stable entry shim. On a healthy build this is a transparent passthrough:
// it loads the real app (app-main.js) exactly as Electron did before. It only acts
// if the real app FAILS TO LOAD (e.g. a module missing from the asar - the v1.2.6
// class), turning a silent crash into a recovery prompt, then exiting non-zero.
// It cannot affect a working build: same __dirname, same requires, same app path.
"use strict";

try {
  require("./app-main.js");
} catch (err) {
  // Keep this branch dependency-free and defensive; anything here runs only when
  // the app was already going to crash, so it can only improve the outcome.
  console.error("Celerp failed to start:", (err && err.stack) || err);
  console.error("RECOVERY: reinstall the latest from https://celerp.com/download");
  try {
    // dialog.showErrorBox is documented safe before the app 'ready' event.
    require("electron").dialog.showErrorBox(
      "Celerp couldn't start",
      "Please reinstall the latest version from https://celerp.com/download\n\n" +
        String((err && err.stack) || err)
    );
  } catch (_) {
    // No dialog available (very early failure); still exit non-zero below.
  }
  process.exit(1);
}
