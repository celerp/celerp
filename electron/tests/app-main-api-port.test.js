// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The data folder's api-port file names the port the API serves on now. A restart
// (after a restore, a module change, the in-app Restart) starts the API on a new
// port, so every start of the API publishes its port, not only the first one.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");

test("every start of the API publishes its port",
  async function test_every_start_of_the_api_publishes_its_port() {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-port-"));
    const childProcess = {
      execFileSync: () => "",
      spawn: () => {
        const server = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter() });
        setImmediate(() => server.emit("error", new Error("test boundary")));
        return server;
      },
    };
    const { startServer } = loadAppMain({
      app: { getPath: () => userData },
      extraFakes: { child_process: childProcess },
    });
    const portFile = path.join(userData, "celerp-data", "api-port");
    fs.mkdirSync(path.dirname(portFile), { recursive: true });
    fs.writeFileSync(portFile, "51969");   // the port of the API before the restart

    await expect(startServer("api", "postgresql://db", {})).rejects.toThrow("test boundary");

    expect(fs.readFileSync(portFile, "utf8")).toBe("9");
  });
