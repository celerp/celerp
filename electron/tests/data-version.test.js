// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The startup check that stops an older Celerp from opening data a newer one
// last opened: the pure decision, and app-main running it before anything
// touches the data directory.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const {
  MARKER_NAME, DOWNLOAD_URL, compareVersions, dataVersionDecision,
  checkDataVersion, guardDataVersion, writeMarker,
} = require("../data-version");
const { loadAppMain } = require("./app-main-loader.lib");

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "celerp-dv-"));
}

describe("compareVersions", () => {
  test("orders release numbers numerically", function test_compare_numeric() {
    expect(compareVersions("2.10.0", "2.9.9")).toBe(1);
    expect(compareVersions("2.5.3", "2.5.3")).toBe(0);
    expect(compareVersions("1.0.0", "2.0.0")).toBe(-1);
  });

  test("puts a prerelease below its release and ignores build metadata",
    function test_compare_prerelease_and_build() {
      expect(compareVersions("2.5.4-dev.3+g1a2b3c", "2.5.4")).toBe(-1);
      expect(compareVersions("2.5.4-dev.10", "2.5.4-dev.9")).toBe(1);
      expect(compareVersions("2.5.4-dev.3+gaaaa", "2.5.4-dev.3+gbbbb")).toBe(0);
      expect(compareVersions("2.5.4-dev.1", "2.5.3")).toBe(1);
    });
});

describe("dataVersionDecision", () => {
  test("refuses data a newer Celerp last opened", function test_decision_newer() {
    expect(dataVersionDecision("2.6.0\n", "2.5.3"))
      .toEqual({ action: "refuse", reason: "newer", dataVersion: "2.6.0" });
  });

  test("opens data this same version last opened", function test_decision_same() {
    expect(dataVersionDecision("2.5.3", "2.5.3")).toEqual({ action: "proceed" });
  });

  test("opens data an older Celerp last opened", function test_decision_older() {
    expect(dataVersionDecision("2.4.0", "2.5.3")).toEqual({ action: "proceed" });
  });

  test("opens a data directory with no record (first run)", function test_decision_missing() {
    expect(dataVersionDecision(null, "2.5.3")).toEqual({ action: "proceed" });
  });

  test("refuses a record that is not a version", function test_decision_corrupt() {
    expect(dataVersionDecision("\u0000garbage", "2.5.3"))
      .toEqual({ action: "refuse", reason: "unreadable" });
    expect(dataVersionDecision("", "2.5.3"))
      .toEqual({ action: "refuse", reason: "unreadable" });
  });
});

describe("guardDataVersion", () => {
  function run(markerContent, runningVersion, choice) {
    const dir = tmpDir();
    const markerPath = path.join(dir, MARKER_NAME);
    if (markerContent !== null) fs.writeFileSync(markerPath, markerContent);
    const shown = [];
    const opened = [];
    const ok = guardDataVersion({
      markerPath,
      runningVersion,
      dialog: { showMessageBoxSync: (opts) => { shown.push(opts); return choice; } },
      shell: { openExternal: (url) => opened.push(url) },
    });
    const after = markerContent === null ? null : fs.readFileSync(markerPath, "utf8");
    return { ok, shown, opened, after, files: fs.readdirSync(dir), markerPath };
  }

  test("tells the user plainly and offers the download page",
    function test_guard_newer_offers_download() {
      const r = run("2.6.0", "2.5.3", 0);
      expect(r.ok).toBe(false);
      expect(r.shown).toHaveLength(1);
      expect(r.shown[0].message).toBe(
        "Your data was last opened with Celerp 2.6.0, which is newer than this copy (2.5.3). " +
        "Download the latest version to continue.");
      expect(r.shown[0].buttons).toEqual(["Download the latest version", "Quit"]);
      expect(r.opened).toEqual([DOWNLOAD_URL]);
      expect(r.after).toBe("2.6.0");
    });

  test("Quit opens nothing and changes nothing", function test_guard_newer_quit() {
    const r = run("2.6.0", "2.5.3", 1);
    expect(r.ok).toBe(false);
    expect(r.opened).toEqual([]);
    expect(r.after).toBe("2.6.0");
    expect(r.files).toEqual([MARKER_NAME]);
  });

  test("a corrupt record stops with a plain message and is left in place",
    function test_guard_corrupt_fails_safe() {
      const r = run("not a version", "2.5.3", 1);
      expect(r.ok).toBe(false);
      expect(r.shown[0].message).toMatch(/has not opened it\. Your data has not been changed\.$/);
      expect(r.shown[0].detail).toContain(r.markerPath);
      expect(r.after).toBe("not a version");
      expect(r.files).toEqual([MARKER_NAME]);
    });

  test("same, older and missing records start without a dialog",
    function test_guard_proceeds_silently() {
      for (const marker of ["2.5.3", "2.4.0", null]) {
        const r = run(marker, "2.5.3", 0);
        expect([marker, r.ok, r.shown.length]).toEqual([marker, true, 0]);
      }
    });

  test("a record that cannot be read is refused, not treated as missing",
    function test_guard_unreadable_record() {
      const dir = tmpDir();
      const markerPath = path.join(dir, MARKER_NAME);
      fs.mkdirSync(markerPath); // reading a directory fails with EISDIR
      expect(checkDataVersion(markerPath, "2.5.3")).toEqual({ action: "refuse", reason: "unreadable" });
    });
});

test("writeMarker records the version whole and leaves no temp file",
  function test_write_marker_atomic() {
    const dir = tmpDir();
    const markerPath = path.join(dir, MARKER_NAME);
    fs.writeFileSync(markerPath, "2.4.0");
    writeMarker(markerPath, "2.5.3");
    expect(fs.readFileSync(markerPath, "utf8")).toBe("2.5.3");
    expect(fs.readdirSync(dir)).toEqual([MARKER_NAME]);
  });

describe("app-main startup", () => {
  // Starts the real app-main.js as a packaged app on a data directory whose
  // record says `marker`. The fake `net` throws, so the first step that would
  // start Postgres or the servers ends the run at a known point.
  async function startPackaged(marker, runningVersion) {
    const userData = tmpDir();
    const moduleDir = path.join(userData, "celerp-data", "modules");
    fs.mkdirSync(moduleDir, { recursive: true });
    const markerPath = path.join(moduleDir, ".default-modules-version");
    fs.writeFileSync(markerPath, marker);
    const events = [];
    let ready;
    const readyDone = new Promise((resolve) => { ready = resolve; });
    loadAppMain({
      app: {
        isPackaged: true,
        getPath: () => userData,
        getVersion: () => runningVersion,
        whenReady: () => ({ then: (fn) => { readyDone.then(() => fn()).then(() => events.push("ready-done")); } }),
        exit: (code) => events.push(`exit:${code}`),
        quit: () => events.push("quit"),
      },
      dialog: {
        showMessageBoxSync: (opts) => { events.push(`message:${opts.message}`); return 1; },
        showErrorBox: (title) => events.push(`error:${title}`),
      },
      shell: { openExternal: (url) => events.push(`open:${url}`) },
      extraFakes: {
        net: { createServer: () => { events.push("net"); throw new Error("test boundary"); } },
      },
      resourcesPath: tmpDir(),
    });
    ready();
    for (let i = 0; i < 50 && !events.includes("ready-done"); i++) {
      await new Promise((r) => setTimeout(r, 10));
    }
    return { events, after: fs.readFileSync(markerPath, "utf8") };
  }

  test("an older copy stops before touching data a newer one opened",
    async function test_app_main_refuses_newer_data_before_startup() {
      const { events, after } = await startPackaged("99.0.0", "2.5.3");
      expect(events).toEqual([
        "message:Your data was last opened with Celerp 99.0.0, which is newer than this copy " +
          "(2.5.3). Download the latest version to continue.",
        "exit:0",
        "ready-done",
      ]);
      expect(after).toBe("99.0.0");
    });

  test("the same version starts as before", async function test_app_main_same_version_starts() {
    const { events } = await startPackaged("2.5.3", "2.5.3");
    expect(events[0]).toBe("net");
    expect(events.some((e) => e.startsWith("message:"))).toBe(false);
  });
});
