// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The data folder's database-url file names the database the API uses now, for the
// password reset command run from a terminal. The bundled database listens on a new
// port each start, so every start of the API publishes the address, readable only by
// the account that runs Celerp because it carries the database password.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { EventEmitter } = require("events");
const { loadAppMain } = require("./app-main-loader.lib");

test("every start of the API publishes its database URL",
  async function test_every_start_of_the_api_publishes_its_database_url() {
    const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-dburl-"));
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
    const urlFile = path.join(userData, "celerp-data", "database-url");
    fs.mkdirSync(path.dirname(urlFile), { recursive: true });
    // The address of the database before the restart, left readable by an older build.
    fs.writeFileSync(urlFile, "postgresql+asyncpg://celerp:pw@localhost:51970/celerp", { mode: 0o644 });

    const now = "postgresql+asyncpg://celerp:pw@localhost:51971/celerp";
    await expect(startServer("api", now, {})).rejects.toThrow("test boundary");

    expect(fs.readFileSync(urlFile, "utf8")).toBe(now);
    if (process.platform !== "win32") {
      expect(fs.statSync(urlFile).mode & 0o777).toBe(0o600);
    }
  });
