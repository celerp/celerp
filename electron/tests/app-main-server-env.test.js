// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The API and UI servers are separate processes that share files in the data folder
// (the outcome of a failed System Recovery, the notice after a restore, the module
// catalog cache). Both must be told where that folder is.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");

test("the UI server is given the same data folder as the API server",
  async function test_the_ui_server_is_given_the_same_data_folder_as_the_api_server() {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-env-"));
    const envs = {};
    const childProcess = {
      execFileSync: () => "",
      spawn: (bin, args, opts) => {
        envs[args.some((a) => a === "ui.app:app") ? "ui" : "api"] = opts.env;
        const server = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter() });
        setImmediate(() => server.emit("error", new Error("test boundary")));
        return server;
      },
    };
    const { startServer } = loadAppMain({
      app: { getPath: () => userData },
      extraFakes: { child_process: childProcess },
    });

    await expect(startServer("api", "postgresql://db", {})).rejects.toThrow("test boundary");
    await expect(startServer("ui", "postgresql://db", {})).rejects.toThrow("test boundary");

    const dataDir = path.join(userData, "celerp-data");
    expect(envs.api.CELERP_DATA_DIR).toBe(dataDir);
    expect(envs.ui.CELERP_DATA_DIR).toBe(dataDir);
  });

test("a UI server that could not start stops the wait for its port",
  async function test_a_ui_server_that_could_not_start_stops_the_wait_for_its_port() {
    let probes = 0;
    const net = {
      ...require("net"),
      createConnection: () => {
        probes += 1;
        const sock = Object.assign(new EventEmitter(), { destroy: () => {} });
        setImmediate(() => sock.emit("error", new Error("refused")));
        return sock;
      },
    };
    const childProcess = {
      execFileSync: () => "",
      spawn: () => {
        const server = Object.assign(new EventEmitter(), { stdout: new EventEmitter(), stderr: new EventEmitter() });
        setImmediate(() => server.emit("error", new Error("could not start")));
        return server;
      },
    };
    const { startServer } = loadAppMain({
      app: { getPath: () => fs.mkdtempSync(path.join(os.tmpdir(), "celerp-env-")) },
      extraFakes: { child_process: childProcess, net },
    });

    await expect(startServer("ui", "postgresql://db", {})).rejects.toThrow("could not start");
    const afterFailure = probes;
    await new Promise((resolve) => setTimeout(resolve, 1200));
    expect(probes).toBeLessThanOrEqual(afterFailure + 1);
  });
