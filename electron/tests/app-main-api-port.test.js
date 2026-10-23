// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The data folder's api-port file names the port the API serves on now. Tooling that
// finds the API there may restart it at once, so the file is written only once the
// app watches the API for a restart (restart.test.js covers when that is), never
// as the API process starts: a restart before that stops the API for good.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");

test("starting the API leaves the port of the API before it in place",
  async function test_starting_the_api_leaves_the_published_port_alone() {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-port-"));
    const childProcess = {
      execFileSync: () => "",
      spawn: () => {
        const server = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter() });
        setImmediate(() => server.emit("error", new Error("test boundary")));
        return server;
      },
    };
    const { startServer, publishApiPort } = loadAppMain({
      app: { getPath: () => userData },
      extraFakes: { child_process: childProcess },
    });
    const portFile = path.join(userData, "celerp-data", "api-port");
    fs.mkdirSync(path.dirname(portFile), { recursive: true });
    fs.writeFileSync(portFile, "51969");   // the port of the API before the restart

    await expect(startServer("api", "postgresql://db", {})).rejects.toThrow("test boundary");
    expect(fs.readFileSync(portFile, "utf8")).toBe("51969");

    publishApiPort();
    expect(fs.readFileSync(portFile, "utf8")).toBe("9");
  });
