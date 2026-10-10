// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Every way the app exits (quitting, a stop signal, installing an update, a full
// relaunch) stops the real servers and then the database, once, before the app exits.

"use strict";

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

async function until(condition) {
  const deadline = Date.now() + 10000;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

const started = [];
const signalListeners = { SIGTERM: process.listeners("SIGTERM"), SIGINT: process.listeners("SIGINT") };
afterEach(() => {
  for (const child of started.splice(0)) if (alive(child)) child.kill("SIGKILL");
  for (const [signal, before] of Object.entries(signalListeners)) {
    for (const fn of process.listeners(signal)) if (!before.includes(fn)) process.removeListener(signal, fn);
  }
});

// Loads app-main with a fake Electron app whose quit() emits before-quit as
// Electron does, and exits only when no handler holds it.
async function running({ stopDatabase = async () => {} } = {}) {
  const events = {};
  const log = [];
  const app = {
    on: (event, fn) => { (events[event] ||= []).push(fn); },
    relaunch: jest.fn(() => log.push("relaunch")),
    quit: jest.fn(() => {
      let held = false;
      for (const fn of events["before-quit"] || []) fn({ preventDefault: () => { held = true; } });
      if (!held) log.push("exit");
    }),
  };
  const loaded = loadAppMain({ app });
  const [api, ui] = await Promise.all([server(), server()]);
  started.push(api, ui);
  const database = {
    stop: jest.fn(async () => {
      log.push(`database stop (servers alive: ${alive(api) || alive(ui)})`);
      await stopDatabase();
    }),
  };
  loaded.setServices(api, ui, database);
  return { ...loaded, app, api, ui, database, log };
}

test("quitting again while the app is stopping stops everything once, then exits",
  async function test_repeated_quits_stop_once_and_exit() {
    const { app, api, ui, database, log } = await running();
    app.quit();
    app.quit();
    expect(log).toEqual([]);
    await until(() => log.includes("exit"));
    app.quit();
    expect([alive(api), alive(ui)]).toEqual([false, false]);
    expect(database.stop).toHaveBeenCalledTimes(1);
    expect(log).toEqual(["database stop (servers alive: false)", "exit", "exit"]);
  });

test("a stop signal, even repeated, runs the same shutdown before the app exits",
  async function test_stop_signals_shut_down_before_exit() {
    const { api, ui, database, log } = await running();
    process.emit("SIGTERM");
    process.emit("SIGINT");
    process.emit("SIGTERM");
    await until(() => log.includes("exit"));
    expect([alive(api), alive(ui)]).toEqual([false, false]);
    expect(database.stop).toHaveBeenCalledTimes(1);
    expect(log).toEqual(["database stop (servers alive: false)", "exit"]);
  });

test("a database that fails to stop does not keep the app from exiting",
  async function test_failed_database_stop_still_exits() {
    const errors = jest.spyOn(console, "error").mockImplementation(() => {});
    try {
      const { app, api, log } = await running({ stopDatabase: async () => { throw new Error("pg stop failed"); } });
      app.quit();
      await until(() => log.includes("exit"));
      expect(alive(api)).toBe(false);
      expect(errors).toHaveBeenCalledWith("[shutdown] database stop failed: pg stop failed");
    } finally {
      errors.mockRestore();
    }
  });

test("installing an update hands over to the installer only after everything has stopped",
  async function test_install_update_waits_for_shutdown() {
    const { app, listeners, updater, api, ui, database, log } = await running();
    updater.quitAndInstall = jest.fn(() => { log.push("install"); app.quit(); });
    listeners["install-update"]();
    await until(() => log.includes("exit"));
    expect([alive(api), alive(ui)]).toEqual([false, false]);
    expect(database.stop).toHaveBeenCalledTimes(1);
    expect(updater.quitAndInstall).toHaveBeenCalledWith(true, true);
    expect(log).toEqual(["database stop (servers alive: false)", "install", "exit"]);
  });

test("a full relaunch stops everything before the app exits and starts again",
  async function test_full_relaunch_waits_for_shutdown() {
    const { listeners, api, ui, database, log } = await running();
    listeners["restart-app"]();
    await until(() => log.includes("exit"));
    expect([alive(api), alive(ui)]).toEqual([false, false]);
    expect(database.stop).toHaveBeenCalledTimes(1);
    expect(log).toEqual(["relaunch", "database stop (servers alive: false)", "exit"]);
  });
