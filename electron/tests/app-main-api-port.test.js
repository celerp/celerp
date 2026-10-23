// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The data folder's api-port file names the port the API serves on now, or is absent.
// Tooling that finds the API there may restart it at once, so the file is written only
// once the app watches the API for a restart (restart.test.js covers when that is),
// never as the API process starts: a restart before that stops the API for good. A
// port of an API that has stopped is never left there either: starting an API first
// takes away the port a previous session or a restart left behind.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");

test("starting the API takes away the port of the API before it and names none until it is watched",
  async function test_starting_the_api_withdraws_the_published_port() {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-port-"));
    const childProcess = {
      execFileSync: () => "",
      spawn: () => {
        const server = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter() });
        setImmediate(() => server.emit("error", new Error("test boundary")));
        return server;
      },
    };
    const { startServer, publishApiPort, withdrawApiPort } = loadAppMain({
      app: { getPath: () => userData },
      extraFakes: { child_process: childProcess },
    });
    const portFile = path.join(userData, "celerp-data", "api-port");
    fs.mkdirSync(path.dirname(portFile), { recursive: true });
    fs.writeFileSync(portFile, "51969");   // the port of an API that has stopped

    await expect(startServer("api", "postgresql://db", {})).rejects.toThrow("test boundary");
    expect(fs.existsSync(portFile)).toBe(false);

    publishApiPort();
    expect(fs.readFileSync(portFile, "utf8")).toBe("9");

    withdrawApiPort();
    expect(fs.existsSync(portFile)).toBe(false);
    withdrawApiPort();   // nothing to take away is not an error
    expect(fs.existsSync(portFile)).toBe(false);
  });
