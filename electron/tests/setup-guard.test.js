// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { setupRunning, SETUP_MUTEX } = require("../setup-guard");

// A TEMP folder, with the pid hint a setup leaves in its plugins folder when `hint` is set.
function temp(hint) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-temp-"));
  fs.mkdirSync(path.join(dir, "nsA1B2.tmp"));
  if (hint) fs.writeFileSync(path.join(dir, "nsA1B2.tmp", "celerp-setup.pid"), "4242");
  return dir;
}

// Runs the guard with the PowerShell probe answering `result` (what spawnSync returns).
function guard({ argv = ["Celerp.exe"], hint = true, result = { status: 1 }, platform = "win32" } = {}) {
  const probes = [];
  const logged = [];
  const running = setupRunning({
    platform, argv, tempDir: temp(hint), log: (line) => logged.push(line),
    spawnSync: (cmd, args, options) => { probes.push({ cmd, args, options }); return result; },
  });
  return { running, probes, logged };
}

test("only a Celerp started by hand while setup runs is held back; every other start goes ahead",
  function test_setup_guard_holds_only_a_start_by_hand() {
    // setup holds its mutex: found, or held by an elevated setup the user cannot open
    const { running, probes } = guard({ result: { status: 0 } });
    expect(running).toBe(true);
    expect(probes.length).toBe(1);
    expect(probes[0].cmd).toBe("powershell.exe");
    expect(probes[0].args.join(" ")).toContain(`[Threading.Mutex]::OpenExisting('${SETUP_MUTEX}')`);
    expect(probes[0].args.join(" ")).toContain("UnauthorizedAccessException");
    expect(probes[0].options.timeout).toBe(3000);
    // started by setup itself: its relaunch, or its reopen after it stopped
    for (const argv of [["Celerp.exe", "--updated"], ["Celerp.exe", "--update-install-failed=2.8.1"]]) {
      expect(guard({ argv, result: { status: 0 } })).toMatchObject({ running: false, probes: [] });
    }
    // a pid hint left by a killed setup, or naming a pid another program now uses: Windows freed the mutex
    expect(guard({ result: { status: 1 } })).toMatchObject({ running: false, logged: [] });
    // the probe cannot run or gives no answer in time: start, and say why in the log
    for (const result of [{ status: null, error: Object.assign(new Error("spawnSync powershell.exe ETIMEDOUT"), { code: "ETIMEDOUT" }) },
                          { status: null, error: Object.assign(new Error("spawnSync powershell.exe ENOENT"), { code: "ENOENT" }) },
                          { status: 2 }]) {
      const { running, logged } = guard({ result });
      expect(running).toBe(false);
      expect(logged.length).toBe(1);
      expect(logged[0]).toMatch(/setup guard/);
    }
    // no hint: no probe
    expect(guard({ hint: false, result: { status: 0 } })).toMatchObject({ running: false, probes: [] });
    // not Windows: no probe
    expect(guard({ platform: "linux", result: { status: 0 } })).toMatchObject({ running: false, probes: [] });
  });

test("the app asks for the same mutex setup creates",
  function test_setup_mutex_name_matches_installer() {
    const nsh = fs.readFileSync(path.join(__dirname, "..", "build", "installer.nsh"), "utf8");
    expect(nsh).toContain(`!define CELERP_SETUP_MUTEX "${SETUP_MUTEX}"`);
    expect(nsh.match(/CELERP_SETUP_MUTEX/g).length).toBe(2);   // the define, and the one place that creates it
    const clean = nsh.split("!macro celerpCleanStoppedSetups")[1].split("!macroend")[0];
    expect(clean).toContain(`System::Call 'kernel32::CreateMutexW(p 0, i 0, w "\${CELERP_SETUP_MUTEX}") p .s'`);
    expect(clean).toContain("Pop $celerpSetupMutex");
  });

test("a Celerp started by hand while setup runs says so and quits before it starts anything",
  async function test_app_quits_while_setup_runs() {
    const { loadAppMain } = require("./app-main-loader.lib");
    const shown = [];
    const quits = [];
    let ready;
    const locks = [];
    loadAppMain({
      app: { whenReady: () => new Promise((resolve) => { ready = resolve; }), quit: () => quits.push(true),
             getLocale: () => "en-US", requestSingleInstanceLock: () => { locks.push(true); return true; } },
      dialog: { showMessageBox: (options) => { shown.push(options); return Promise.resolve({ response: 0 }); } },
      extraFakes: { "./setup-guard": { ...require("../setup-guard"), setupRunning: () => true } },
      platform: "win32",
    });
    expect(locks).toEqual([]);
    ready();
    await new Promise((resolve) => setImmediate(resolve));
    await new Promise((resolve) => setImmediate(resolve));
    expect(shown.length).toBe(1);
    expect(shown[0].message).toBe("Celerp is being updated. It opens by itself when the update is done.");
    expect(quits).toEqual([true]);
  });
