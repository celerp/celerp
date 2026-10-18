// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Delete All Data on Windows: the servers and the database stop first, and only
// then are the data folder and the private database folder deleted. A database
// that does not stop means nothing is deleted, and anything that cannot be deleted
// is shown to the user, not only logged.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");
const { loadAppMain } = require("./app-main-loader.lib");

function server() {
  const child = spawn(process.execPath, ["-e", "console.log('ready'); setInterval(() => {}, 1000);"],
    { stdio: ["ignore", "pipe", "ignore"] });
  return new Promise((resolve) => child.stdout.once("data", () => resolve(child)));
}

function alive(child) {
  try { process.kill(child.pid, 0); return true; } catch { return false; }
}

const started = [];
const scratch = [];
afterEach(() => {
  for (const child of started.splice(0)) if (alive(child)) child.kill("SIGKILL");
  for (const dir of scratch.splice(0)) {
    fs.chmodSync(dir, 0o755);
    for (const entry of fs.readdirSync(dir)) {
      try { fs.chmodSync(path.join(dir, entry), 0o755); } catch { /* a dangling link */ }
    }
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

// A packaged Windows app whose user data and private database folder hold real
// files, with its servers running and a database that records what was on disk
// when it was asked to stop.
async function installed({ stopDatabase = async () => {}, confirm = 1 } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-delete-"));
  scratch.push(root);
  const userData = path.join(root, "userData");
  const dataDir = path.join(userData, "celerp-data");
  const programData = path.join(root, "ProgramData");
  const privateRoot = path.join(programData, "Celerp-S-1-5-21-9");
  const program = path.join(root, "Programs", "Celerp", "native");
  for (const dir of [path.join(dataDir, "logs"), path.join(privateRoot, "postgres"), program]) {
    fs.mkdirSync(dir, { recursive: true });
  }
  fs.writeFileSync(path.join(dataDir, "logs", "api.log"), "log");
  fs.writeFileSync(path.join(privateRoot, "postgres", "PG_VERSION"), "17");
  fs.writeFileSync(path.join(program, "postgres.exe"), "program");
  fs.symlinkSync(program, path.join(privateRoot, "native"), "junction");

  const log = [];
  const shown = [];
  const app = {
    isPackaged: true,
    getPath: () => userData,
    quit: jest.fn(() => log.push("quit")),
  };
  const dialog = {
    showMessageBox: jest.fn(async (_win, options) => {
      shown.push(options);
      return { response: shown.length === 1 ? confirm : 0 };
    }),
  };
  const windowsInitdb = {
    ...require("../windows-initdb"),
    postgresPaths: () => ({ dataDir: path.join(privateRoot, "postgres"), privateRoot }),
  };
  const loaded = loadAppMain({ app, dialog, platform: "win32", resourcesPath: path.join(root, "resources"),
                               extraFakes: { "./windows-initdb": windowsInitdb } });
  const [api, ui] = await Promise.all([server(), server()]);
  started.push(api, ui);
  const database = {
    stop: jest.fn(async () => {
      log.push(`database stop (servers alive: ${alive(api) || alive(ui)}, ` +
               `data there: ${fs.existsSync(dataDir) && fs.existsSync(privateRoot)})`);
      await stopDatabase();
    }),
  };
  loaded.setServices(api, ui, database);
  const deleteAll = () => loaded.handlers["uninstall-delete-data"]();
  return { root, dataDir, privateRoot, programData, program, api, ui, database, log, shown, app, deleteAll };
}

test("everything stops before the data folder and the private database folder are deleted",
  async function test_delete_all_data_stops_then_deletes_everything() {
    const { dataDir, privateRoot, program, api, ui, database, log, shown, deleteAll } = await installed();
    await deleteAll();
    expect(shown[0].detail).toContain(`• ${dataDir}\n`);
    expect(shown[0].detail).toContain(`• ${privateRoot}\n`);
    expect([alive(api), alive(ui)]).toEqual([false, false]);
    expect(database.stop).toHaveBeenCalledTimes(1);
    expect(log).toEqual(["database stop (servers alive: false, data there: true)", "quit"]);
    expect([fs.existsSync(dataDir), fs.existsSync(privateRoot)]).toEqual([false, false]);
    // The link to the program folder went as a link; the program folder stays.
    expect(fs.readFileSync(path.join(program, "postgres.exe"), "utf8")).toBe("program");
    expect(shown).toHaveLength(1);
  });

test("nothing is deleted when the database does not stop, and the user is told",
  async function test_delete_all_data_keeps_everything_when_shutdown_fails() {
    const errors = jest.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { dataDir, privateRoot, log, shown, deleteAll } = await installed({
        stopDatabase: async () => { throw new Error("pg stop failed"); },
      });
      await deleteAll();
      expect(fs.readFileSync(path.join(dataDir, "logs", "api.log"), "utf8")).toBe("log");
      expect(fs.readFileSync(path.join(privateRoot, "postgres", "PG_VERSION"), "utf8")).toBe("17");
      expect(shown).toHaveLength(2);
      expect(shown[1].type).toBe("error");
      expect(shown[1].message).toBe("Nothing was deleted");
      expect(log[log.length - 1]).toBe("quit");
    } finally {
      errors.mockRestore();
    }
  });

test("a folder that cannot be deleted is shown to the user, and the rest is still deleted",
  async function test_delete_all_data_shows_what_it_could_not_delete() {
    const errors = jest.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { dataDir, privateRoot, programData, shown, log, deleteAll } = await installed();
      fs.chmodSync(programData, 0o555);  // the private folder cannot be removed from it
      await deleteAll();
      expect(fs.existsSync(dataDir)).toBe(false);
      expect(fs.existsSync(privateRoot)).toBe(true);
      expect(shown).toHaveLength(2);
      expect(shown[1].type).toBe("error");
      expect(shown[1].message).toBe("Some Celerp data was not deleted");
      expect(shown[1].detail).toContain(privateRoot);
      expect(shown[1].detail).not.toContain(`${dataDir}:`);
      expect(log[log.length - 1]).toBe("quit");
    } finally {
      errors.mockRestore();
    }
  });

test("cancelling stops nothing and deletes nothing",
  async function test_delete_all_data_cancel_changes_nothing() {
    const { dataDir, privateRoot, api, database, app, deleteAll } = await installed({ confirm: 0 });
    await deleteAll();
    expect(alive(api)).toBe(true);
    expect(database.stop).not.toHaveBeenCalled();
    expect(app.quit).not.toHaveBeenCalled();
    expect([fs.existsSync(dataDir), fs.existsSync(privateRoot)]).toEqual([true, true]);
  });
