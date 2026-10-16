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

function start({ userData, token, explorerError = null, netSessionWorks = false }) {
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
  const spawnSync = jest.fn(() => ({ status: 1, error: explorerError || undefined }));
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

test("an elevated relaunched copy (Explorer itself elevated) boots restricted, never a loop", () => {
  const userData = userDataDir();
  expect(start({ userData, token: "elevated" }).relaunched).toBe(true);
  const second = start({ userData, token: "elevated" });
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

test("Linux and macOS keep running Postgres under desktop_child.py", () => {
  const { postgresLauncher } = loadAppMain({ platform: "linux" });
  expect(postgresLauncher()[2]).toMatch(/desktop_child\.py$/);
});
