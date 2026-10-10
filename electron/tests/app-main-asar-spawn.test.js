// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The database binary lives in app.asar.unpacked. When the app starts it through a
// launcher, the binary is an argument, so its path must point there too.

"use strict";

const { loadAppMain } = require("./app-main-loader.lib");

test("a program in app.asar started through a launcher gets its unpacked path",
  function test_a_program_in_app_asar_started_through_a_launcher_gets_its_unpacked_path() {
    const spawn = jest.fn();
    const childProcess = { spawn, execFileSync: () => "" };
    loadAppMain({ extraFakes: { child_process: childProcess } });

    childProcess.spawn("/R/python-arm64/python/bin/python3",
      ["-I", "/R/app/celerp/desktop_child.py", "/R/app.asar/node_modules/@embedded-postgres/darwin-arm64/native/bin/postgres", "-D", "/data"],
      { env: {} });

    expect(spawn).toHaveBeenCalledWith("/R/python-arm64/python/bin/python3",
      ["-I", "/R/app/celerp/desktop_child.py", "/R/app.asar.unpacked/node_modules/@embedded-postgres/darwin-arm64/native/bin/postgres", "-D", "/data"],
      { env: {} });
  });
