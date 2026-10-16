// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// An elevated Windows start relaunches through Explorer only when that can drop
// the administrator rights (UAC on), at most once; an administrator token with no
// standard token behind it (UAC off, built-in Administrator) boots in place.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { MARKER_MAX_AGE_MS, deelevationAction, startupDeelevation, relaunchThroughExplorer } = require("../deelevation");

const NOW = 1_800_000_000_000;

function markerIn(dir = fs.mkdtempSync(path.join(os.tmpdir(), "deelevate-"))) {
  return path.join(dir, "deelevate-attempt");
}

describe("deelevationAction", () => {
  test("a standard token boots normally", () => {
    expect(deelevationAction({ token: "standard", markerAgeMs: null })).toBe("boot");
    expect(deelevationAction({ token: "standard", markerAgeMs: 1000 })).toBe("boot");
  });

  test("an elevated token with a standard one behind it (UAC on) relaunches", () => {
    expect(deelevationAction({ token: "elevated", markerAgeMs: null })).toBe("relaunch");
  });

  test("an administrator token with no standard one behind it (UAC off) boots restricted without relaunching", () => {
    expect(deelevationAction({ token: "admin", markerAgeMs: null })).toBe("boot-restricted");
  });

  test("a token that could not be read boots restricted, which works for every account", () => {
    expect(deelevationAction({ token: null, markerAgeMs: null })).toBe("boot-restricted");
  });

  test("elevated with a fresh marker boots restricted instead of relaunching again", () => {
    expect(deelevationAction({ token: "elevated", markerAgeMs: 0 })).toBe("boot-restricted");
    expect(deelevationAction({ token: "elevated", markerAgeMs: MARKER_MAX_AGE_MS - 1 })).toBe("boot-restricted");
  });

  test("elevated with a stale or future-dated marker relaunches", () => {
    expect(deelevationAction({ token: "elevated", markerAgeMs: MARKER_MAX_AGE_MS })).toBe("relaunch");
    expect(deelevationAction({ token: "elevated", markerAgeMs: -5000 })).toBe("relaunch");
  });

  test("the marker counts for one minute", () => {
    expect(MARKER_MAX_AGE_MS).toBe(60 * 1000);
  });
});

describe("startupDeelevation", () => {
  test("a relaunched copy that is still elevated boots restricted, never a loop", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW })).toBe("relaunch");
    expect(fs.readFileSync(markerPath, "utf8")).toBe(String(NOW));
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW + 3000 })).toBe("boot-restricted");
    expect(fs.existsSync(markerPath)).toBe(false);
  });

  test("UAC on: the relaunched copy has a standard token, boots normally and clears the marker", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW })).toBe("relaunch");
    expect(startupDeelevation({ token: "standard", markerPath, now: NOW + 3000 })).toBe("boot");
    expect(fs.existsSync(markerPath)).toBe(false);
    // A later "Run as administrator" relaunches again.
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW + 10_000 })).toBe("relaunch");
  });

  test("UAC off never writes a marker", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ token: "admin", markerPath, now: NOW })).toBe("boot-restricted");
    expect(fs.existsSync(markerPath)).toBe(false);
  });

  test("a stale marker from an earlier run is ignored and replaced", () => {
    const markerPath = markerIn();
    fs.writeFileSync(markerPath, String(NOW - MARKER_MAX_AGE_MS - 1));
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW })).toBe("relaunch");
    expect(fs.readFileSync(markerPath, "utf8")).toBe(String(NOW));
  });

  test("an unreadable marker counts as none", () => {
    const markerPath = markerIn();
    fs.writeFileSync(markerPath, "garbage");
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW })).toBe("relaunch");
  });

  test("the user data folder is created for the marker on a first start", () => {
    const markerPath = markerIn(path.join(fs.mkdtempSync(path.join(os.tmpdir(), "deelevate-")), "Celerp"));
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW })).toBe("relaunch");
    expect(fs.existsSync(markerPath)).toBe(true);
  });

  test("a marker that cannot be written boots restricted instead of relaunching", () => {
    const blocker = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "deelevate-")), "file");
    fs.writeFileSync(blocker, "");
    // The marker's folder is a file, so the marker cannot be written.
    const markerPath = path.join(blocker, "deelevate-attempt");
    expect(startupDeelevation({ token: "elevated", markerPath, now: NOW })).toBe("boot-restricted");
  });

  test("a standard token never writes a marker", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ token: "standard", markerPath, now: NOW })).toBe("boot");
    expect(fs.existsSync(markerPath)).toBe(false);
  });
});

describe("relaunchThroughExplorer", () => {
  const EXE = "C:\\Program Files\\Celerp\\Celerp.exe";

  test("hands the app to Explorer and reports success once Explorer has taken it", () => {
    const spawnSync = jest.fn(() => ({ status: 1, error: undefined }));
    expect(relaunchThroughExplorer({ spawnSync, systemRoot: "C:\\Windows", exePath: EXE })).toBe(true);
    const [cmd, args, options] = spawnSync.mock.calls[0];
    expect(cmd).toMatch(/explorer\.exe$/);
    expect(args).toEqual([EXE]);
    expect(options.timeout).toBeGreaterThan(0);
  });

  test("Explorer failing to start is reported, not lost", () => {
    const error = Object.assign(new Error("spawn explorer.exe ENOENT"), { code: "ENOENT" });
    const spawnSync = jest.fn(() => ({ status: null, error }));
    expect(relaunchThroughExplorer({ spawnSync, systemRoot: "C:\\Windows", exePath: EXE })).toBe(false);
  });

  test("Explorer not handing off in time is reported", () => {
    const error = Object.assign(new Error("spawnSync explorer.exe ETIMEDOUT"), { code: "ETIMEDOUT" });
    expect(relaunchThroughExplorer({ spawnSync: () => ({ status: null, error }), systemRoot: "C:\\Windows", exePath: EXE }))
      .toBe(false);
  });

  test("a spawn that throws is reported", () => {
    const spawnSync = () => { throw new Error("EPERM"); };
    expect(relaunchThroughExplorer({ spawnSync, systemRoot: "C:\\Windows", exePath: EXE })).toBe(false);
  });
});
