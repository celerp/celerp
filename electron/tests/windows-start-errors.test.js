// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// A database that cannot start on Windows tells the user what to do about it.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { windowsStartFailure, logSince } = require("../windows-start-errors");

const VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe";

describe("windowsStartFailure", () => {
  test("a missing DLL (0xC0000135) points at the Visual C++ runtime, as Node reports it", () => {
    const message = windowsStartFailure({ exitCode: 3221225781 });
    expect(message).toContain("A Windows component Celerp needs is missing.");
    expect(message).toContain("Microsoft Visual C++ Redistributable (x64)");
    expect(message).toContain(VC_REDIST);
  });

  test("a missing DLL read as a signed exit code gives the same message", () => {
    expect(windowsStartFailure({ exitCode: -1073741515 })).toBe(windowsStartFailure({ exitCode: 3221225781 }));
  });

  test("Postgres refusing an administrator token asks for UAC or a standard account", () => {
    const output = "Execution of PostgreSQL by a user with administrative permissions is not\npermitted.\n";
    expect(windowsStartFailure({ exitCode: 1, output })).toBe(
      "Celerp cannot start from an administrator account with User Account Control turned off. " +
      "Turn on UAC, or sign in with a standard Windows account.");
  });

  test("the restricted launcher failing gives the same UAC message", () => {
    const output = "win_restricted_launch: CreateRestrictedToken failed (error 5)\n";
    expect(windowsStartFailure({ exitCode: 1, output })).toContain("Turn on UAC");
  });

  test("initdb failing to drop the administrator rights gives the same UAC message", () => {
    const output = "initdb: error: could not re-execute with restricted token: error code 5\n";
    expect(windowsStartFailure({ exitCode: 1, output })).toContain("Turn on UAC");
  });

  test("any other failure keeps its original error", () => {
    expect(windowsStartFailure({ exitCode: 1, output: "FATAL: could not bind IPv4 address" })).toBeNull();
    expect(windowsStartFailure({ exitCode: null })).toBeNull();
  });

  test("no message carries an em dash", () => {
    expect(windowsStartFailure({ exitCode: 3221225781 })).not.toMatch(/\u2014/);
    expect(windowsStartFailure({ exitCode: 1, output: "administrative permissions" })).not.toMatch(/\u2014/);
  });
});

describe("logSince", () => {
  test("returns only what was written to the log after the offset", () => {
    const logPath = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "initdb-log-")), "initdb-win.log");
    fs.writeFileSync(logPath, "an earlier start: administrative permissions\n");
    const offset = fs.statSync(logPath).size;
    fs.appendFileSync(logPath, "initdb: error: could not create directory\n");
    expect(logSince(logPath, offset)).toBe("initdb: error: could not create directory\n");
  });

  test("a log that does not exist reads as empty", () => {
    expect(logSince(path.join(os.tmpdir(), "no-such-dir-celerp", "x.log"), 0)).toBe("");
  });
});
