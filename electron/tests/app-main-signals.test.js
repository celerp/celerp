// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// A stop signal from the system (logout, shutdown, kill) runs the same quit
// path as closing the app, so the servers and database stop with it.

"use strict";

const { loadAppMain } = require("./app-main-loader.lib");

test.each(["SIGTERM", "SIGINT"])("app-main quits the app on %s",
  function test_app_main_quits_the_app_on_a_stop_signal(signal) {
    const before = process.listeners(signal);
    const quit = jest.fn();
    loadAppMain({ app: { quit } });
    try {
      process.emit(signal);
      expect(quit).toHaveBeenCalled();
    } finally {
      for (const fn of process.listeners(signal)) {
        if (!before.includes(fn)) process.removeListener(signal, fn);
      }
    }
  });
