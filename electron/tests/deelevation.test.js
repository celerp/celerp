// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// An elevated Windows start relaunches through Explorer at most once: with UAC off
// the relaunched copy is elevated again and must boot in place, never relaunch.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { MARKER_MAX_AGE_MS, deelevationAction, startupDeelevation } = require("../deelevation");

const NOW = 1_800_000_000_000;

function markerIn(dir = fs.mkdtempSync(path.join(os.tmpdir(), "deelevate-"))) {
  return path.join(dir, "deelevate-attempt");
}

describe("deelevationAction", () => {
  test("not elevated boots normally", () => {
    expect(deelevationAction({ elevated: false, markerAgeMs: null })).toBe("boot");
    expect(deelevationAction({ elevated: false, markerAgeMs: 1000 })).toBe("boot");
  });

  test("elevated with no marker relaunches", () => {
    expect(deelevationAction({ elevated: true, markerAgeMs: null })).toBe("relaunch");
  });

  test("elevated with a fresh marker boots restricted instead of relaunching again", () => {
    expect(deelevationAction({ elevated: true, markerAgeMs: 0 })).toBe("boot-restricted");
    expect(deelevationAction({ elevated: true, markerAgeMs: MARKER_MAX_AGE_MS - 1 })).toBe("boot-restricted");
  });

  test("elevated with a stale or future-dated marker relaunches", () => {
    expect(deelevationAction({ elevated: true, markerAgeMs: MARKER_MAX_AGE_MS })).toBe("relaunch");
    expect(deelevationAction({ elevated: true, markerAgeMs: -5000 })).toBe("relaunch");
  });

  test("the marker counts for one minute", () => {
    expect(MARKER_MAX_AGE_MS).toBe(60 * 1000);
  });
});

describe("startupDeelevation", () => {
  test("UAC off: the first start relaunches, the relaunched copy boots restricted, never a loop", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW })).toBe("relaunch");
    expect(fs.readFileSync(markerPath, "utf8")).toBe(String(NOW));
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW + 3000 })).toBe("boot-restricted");
    expect(fs.existsSync(markerPath)).toBe(false);
  });

  test("UAC on: the relaunched copy is not elevated, boots normally and clears the marker", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW })).toBe("relaunch");
    expect(startupDeelevation({ elevated: false, markerPath, now: NOW + 3000 })).toBe("boot");
    expect(fs.existsSync(markerPath)).toBe(false);
    // A later "Run as administrator" relaunches again.
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW + 10_000 })).toBe("relaunch");
  });

  test("a stale marker from an earlier run is ignored and replaced", () => {
    const markerPath = markerIn();
    fs.writeFileSync(markerPath, String(NOW - MARKER_MAX_AGE_MS - 1));
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW })).toBe("relaunch");
    expect(fs.readFileSync(markerPath, "utf8")).toBe(String(NOW));
  });

  test("an unreadable marker counts as none", () => {
    const markerPath = markerIn();
    fs.writeFileSync(markerPath, "garbage");
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW })).toBe("relaunch");
  });

  test("the user data folder is created for the marker on a first start", () => {
    const markerPath = markerIn(path.join(fs.mkdtempSync(path.join(os.tmpdir(), "deelevate-")), "Celerp"));
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW })).toBe("relaunch");
    expect(fs.existsSync(markerPath)).toBe(true);
  });

  test("a marker that cannot be written boots restricted instead of relaunching", () => {
    const blocker = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "deelevate-")), "file");
    fs.writeFileSync(blocker, "");
    // The marker's folder is a file, so the marker cannot be written.
    const markerPath = path.join(blocker, "deelevate-attempt");
    expect(startupDeelevation({ elevated: true, markerPath, now: NOW })).toBe("boot-restricted");
  });

  test("not elevated never writes a marker", () => {
    const markerPath = markerIn();
    expect(startupDeelevation({ elevated: false, markerPath, now: NOW })).toBe("boot");
    expect(fs.existsSync(markerPath)).toBe(false);
  });
});
