// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// An elevated Windows start relaunches through Explorer once. With UAC off the
// relaunched copy is elevated again: it boots in place and starts Postgres through
// the restricted launcher instead of relaunching forever.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const childProcessReal = require("child_process");
const { loadAppMain } = require("./app-main-loader.lib");

function start({ userData, elevated }) {
  const spawn = jest.fn(() => ({ on: () => {}, unref: () => {} }));
  const execSync = jest.fn(() => { if (!elevated) throw new Error("access denied"); });
  const quit = jest.fn();
  const requestSingleInstanceLock = jest.fn(() => true);
  const app = loadAppMain({
    platform: "win32",
    app: { getPath: () => userData, quit, requestSingleInstanceLock },
    extraFakes: { child_process: { ...childProcessReal, spawn, execSync } },
  });
  const relaunched = spawn.mock.calls.some(([cmd]) => /explorer\.exe$/.test(cmd));
  return { ...app, relaunched, quit, requestSingleInstanceLock };
}

const RESTRICTED = /win_restricted_launch\.py$/;

test("UAC off: the elevated start relaunches once, the relaunched copy boots with Postgres restricted", () => {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-userdata-"));

  const first = start({ userData, elevated: true });
  expect(first.relaunched).toBe(true);
  expect(first.quit).toHaveBeenCalled();
  expect(first.requestSingleInstanceLock).not.toHaveBeenCalled();

  const second = start({ userData, elevated: true });
  expect(second.relaunched).toBe(false);
  expect(second.quit).not.toHaveBeenCalled();
  expect(second.requestSingleInstanceLock).toHaveBeenCalled();
  const launcher = second.postgresLauncher();
  expect(launcher[1]).toBe("-I");
  expect(launcher[2]).toMatch(RESTRICTED);
});

test("UAC on: the relaunched copy is not elevated and starts Postgres directly, as before", () => {
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-userdata-"));
  expect(start({ userData, elevated: true }).relaunched).toBe(true);
  const relaunchedCopy = start({ userData, elevated: false });
  expect(relaunchedCopy.relaunched).toBe(false);
  expect(relaunchedCopy.postgresLauncher()).toEqual([]);
});

test("a marker that cannot be written boots restricted instead of relaunching", () => {
  const blocker = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "celerp-userdata-")), "file");
  fs.writeFileSync(blocker, "");
  const started = start({ userData: blocker, elevated: true });
  expect(started.relaunched).toBe(false);
  expect(started.postgresLauncher()[2]).toMatch(RESTRICTED);
});

test("Linux and macOS keep running Postgres under desktop_child.py", () => {
  const { postgresLauncher } = loadAppMain({ platform: "linux" });
  expect(postgresLauncher()[2]).toMatch(/desktop_child\.py$/);
});
