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
