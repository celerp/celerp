// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The Windows start reads the administrator state from the app's own access token.
// An elevated start with UAC on relaunches through Explorer once; an administrator
// start with UAC off boots in place and starts Postgres through the restricted
// launcher. A relaunch that Explorer cannot take boots restricted instead of
// quitting with nothing running.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const childProcessReal = require("child_process");
const { loadAppMain } = require("./app-main-loader.lib");

// `copyStarts` is what the copy Explorer starts does; by default it consumes the
// relaunch marker as a real copy does when it starts.
const consumeMarker = (userData) => () => fs.rmSync(path.join(userData, "deelevate-attempt"), { force: true });

function start({ userData, token, explorerError = null, explorerSignal = null, netSessionWorks = false,
                 copyStarts = consumeMarker(userData) }) {
  const execFileSync = jest.fn((cmd, args) => {
    if (args && args.includes("--token-state")) {
      if (token === null) throw new Error("token query failed");
      return `${token}\n`;
    }
    return childProcessReal.execFileSync(cmd, args);
  });
  // `net session` is not how the state is read: it fails whenever the Server
  // service is stopped, whatever the token says.
  const execSync = jest.fn(() => { if (!netSessionWorks) throw new Error("service not started"); });
  // Explorer starts the relaunched copy, which consumes the relaunch marker as it starts.
  const spawnSync = jest.fn(() => {
    if (!explorerError) copyStarts();
    return { status: explorerSignal ? null : 1, signal: explorerSignal, error: explorerError || undefined };
  });
  const spawn = jest.fn(() => ({ on: () => {}, unref: () => {} }));
  const quit = jest.fn();
  const requestSingleInstanceLock = jest.fn(() => true);
  const app = loadAppMain({
    platform: "win32",
    app: { getPath: () => userData, quit, requestSingleInstanceLock },
    extraFakes: { child_process: { ...childProcessReal, spawn, spawnSync, execSync, execFileSync } },
  });
  const explorerCalls = [...spawnSync.mock.calls, ...spawn.mock.calls].filter(([cmd]) => /explorer\.exe$/.test(cmd));
  return { ...app, relaunched: explorerCalls.length > 0, quit, requestSingleInstanceLock, execFileSync };
}

const RESTRICTED = /win_restricted_launch\.py$/;
const userDataDir = () => fs.mkdtempSync(path.join(os.tmpdir(), "celerp-userdata-"));

test("the token state is read through the bundled Python with the restricted launcher script", () => {
  const started = start({ userData: userDataDir(), token: "standard" });
  const call = started.execFileSync.mock.calls.find(([, args]) => args && args.includes("--token-state"));
  expect(call).toBeDefined();
  expect(call[1][0]).toBe("-I");
  expect(call[1][1]).toMatch(RESTRICTED);
});

test("UAC off with the Server service stopped: boots in place with Postgres restricted, no relaunch", () => {
  const started = start({ userData: userDataDir(), token: "admin", netSessionWorks: false });
  expect(started.relaunched).toBe(false);
  expect(started.quit).not.toHaveBeenCalled();
  expect(started.requestSingleInstanceLock).toHaveBeenCalled();
  const launcher = started.postgresLauncher();
  expect(launcher[1]).toBe("-I");
  expect(launcher[2]).toMatch(RESTRICTED);
});

test("UAC on, Run as administrator: relaunches through Explorer and quits only after Explorer took it", () => {
  const userData = userDataDir();
  const first = start({ userData, token: "elevated" });
  expect(first.relaunched).toBe(true);
  expect(first.quit).toHaveBeenCalled();
  expect(first.requestSingleInstanceLock).not.toHaveBeenCalled();

  const relaunchedCopy = start({ userData, token: "standard" });
  expect(relaunchedCopy.relaunched).toBe(false);
  expect(relaunchedCopy.postgresLauncher()).toEqual([]);
});

test("Explorer failing to take the relaunch boots restricted instead of quitting", () => {
  const explorerError = Object.assign(new Error("spawn explorer.exe ENOENT"), { code: "ENOENT" });
  const started = start({ userData: userDataDir(), token: "elevated", explorerError });
  expect(started.relaunched).toBe(true);
  expect(started.quit).not.toHaveBeenCalled();
  expect(started.requestSingleInstanceLock).toHaveBeenCalled();
  expect(started.postgresLauncher()[2]).toMatch(RESTRICTED);
});

test("Explorer ended by a signal boots restricted instead of quitting", () => {
  const started = start({ userData: userDataDir(), token: "elevated", explorerSignal: "SIGTERM" });
  expect(started.relaunched).toBe(true);
  expect(started.quit).not.toHaveBeenCalled();
  expect(started.requestSingleInstanceLock).toHaveBeenCalled();
  expect(started.postgresLauncher()[2]).toMatch(RESTRICTED);
});

test("an elevated relaunched copy (Explorer itself elevated) boots restricted, never a loop", () => {
  const userData = userDataDir();
  let second;
  const first = start({ userData, token: "elevated", copyStarts: () => { second = start({ userData, token: "elevated" }); } });
  expect(first.relaunched).toBe(true);
  expect(first.quit).toHaveBeenCalled();
  expect(second.relaunched).toBe(false);
  expect(second.quit).not.toHaveBeenCalled();
  expect(second.postgresLauncher()[2]).toMatch(RESTRICTED);
});

test("a token that cannot be read boots restricted", () => {
  const started = start({ userData: userDataDir(), token: null });
  expect(started.relaunched).toBe(false);
  expect(started.postgresLauncher()[2]).toMatch(RESTRICTED);
});

test("a standard account boots as before", () => {
  const started = start({ userData: userDataDir(), token: "standard" });
  expect(started.relaunched).toBe(false);
  expect(started.postgresLauncher()).toEqual([]);
});

test("UAC off: the API and UI servers start through the restricted launcher too", () => {
  const started = start({ userData: userDataDir(), token: "admin" });
  const [command, args] = started.serverCommand("celerp.main:app", 8000);
  expect(args[0]).toBe("-I");
  expect(args[1]).toMatch(RESTRICTED);
  expect(args[2]).toBe(command);
  expect(args.slice(3)).toEqual(["-m", "uvicorn", "celerp.main:app", "--host", "127.0.0.1", "--port", "8000",
    "--timeout-graceful-shutdown", "3"]);
});

test("a standard account starts the servers directly", () => {
  const started = start({ userData: userDataDir(), token: "standard" });
  const [, args] = started.serverCommand("ui.app:app", 8001);
  expect(args.slice(0, 3)).toEqual(["-m", "uvicorn", "ui.app:app"]);
});

test("Linux and macOS keep running Postgres under desktop_child.py", () => {
  const { postgresLauncher } = loadAppMain({ platform: "linux" });
  expect(postgresLauncher()[2]).toMatch(/desktop_child\.py$/);
});
