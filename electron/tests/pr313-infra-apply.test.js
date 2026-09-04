// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// PR #313: packaged-build storage-mode decision, storage persist-back-to-local,
// full-relaunch restart, and the restart-app preload bridge.
"use strict";

// Preload requires the real electron module; stub it so the bridge and IPC
// channel can be observed without an Electron runtime.
const ipcSent = [];
let exposed = null;
jest.mock(
  "electron",
  () => ({
    contextBridge: {
      exposeInMainWorld: (name, api) => {
        exposed = { name, api };
      },
    },
    ipcRenderer: {
      send: (channel, ...args) => ipcSent.push([channel, ...args]),
      invoke: () => Promise.resolve(),
      on: () => {},
      sendSync: () => undefined,
    },
  }),
  { virtual: true },
);

const dbmode = require("../db-mode");
const restart = require("../restart");

const DAY_MS = 24 * 60 * 60 * 1000;
const future = () => new Date(Date.now() + DAY_MS).toISOString();
const past = () => new Date(Date.now() - DAY_MS).toISOString();

// ── storageModeDecision ───────────────────────────────────────────────────────

describe("storageModeDecision", () => {
  test("test_storage_mode_decision_start_and_persist: entitled+s3 -> startS3; expired s3 -> persistLocal", () => {
    const entitled = {
      storage_mode: "s3",
      storage_s3_bucket: "team-bucket",
      feature_flags: { external_db: false, external_storage: true, grace_period_ends: null },
    };
    const active = dbmode.storageModeDecision(entitled);
    expect(active.startS3).toBe(true);
    expect(active.persistLocal).toBe(false);
    expect(active.gracePeriod).toBe(false);

    const expired = {
      storage_mode: "s3",
      storage_s3_bucket: "team-bucket",
      feature_flags: { external_db: false, external_storage: false, grace_period_ends: past() },
    };
    const gone = dbmode.storageModeDecision(expired);
    expect(gone.startS3).toBe(false);
    expect(gone.persistLocal).toBe(true);

    const grace = {
      storage_mode: "s3",
      storage_s3_bucket: "team-bucket",
      feature_flags: { external_db: false, external_storage: false, grace_period_ends: future() },
    };
    const inGrace = dbmode.storageModeDecision(grace);
    expect(inGrace.startS3).toBe(true);
    expect(inGrace.gracePeriod).toBe(true);
    expect(inGrace.persistLocal).toBe(false);
  });
});

// ── applyStoragePersist ───────────────────────────────────────────────────────

describe("applyStoragePersist", () => {
  test("test_apply_storage_persist_flips_local_when_expired: writes storage_mode=local, returns true", () => {
    const writeConfigFn = jest.fn();
    const cfg = { storage_mode: "s3", storage_s3_bucket: "team-bucket" };
    const persisted = dbmode.applyStoragePersist(cfg, { persistLocal: true }, writeConfigFn);
    expect(persisted).toBe(true);
    expect(writeConfigFn).toHaveBeenCalledTimes(1);
    expect(writeConfigFn).toHaveBeenCalledWith({ storage_mode: "local" });
  });

  test("test_apply_storage_persist_preserves_s3_settings: patch carries only storage_mode", () => {
    const writeConfigFn = jest.fn();
    const cfg = {
      storage_mode: "s3",
      storage_s3_endpoint: "https://s3.example.com",
      storage_s3_bucket: "team-bucket",
      storage_s3_access_key: "AKIA",
      storage_s3_secret_key: "secret",
    };
    dbmode.applyStoragePersist(cfg, { persistLocal: true }, writeConfigFn);
    const patch = writeConfigFn.mock.calls[0][0];
    expect(Object.keys(patch)).toEqual(["storage_mode"]);
    expect(patch).not.toHaveProperty("storage_s3_bucket");
    expect(patch).not.toHaveProperty("storage_s3_secret_key");
  });

  test("test_apply_storage_persist_noop_when_allowed: no write when allowed/in grace", () => {
    const writeConfigFn = jest.fn();
    expect(dbmode.applyStoragePersist({}, { persistLocal: false }, writeConfigFn)).toBe(false);
    expect(writeConfigFn).not.toHaveBeenCalled();
  });

  test("test_apply_storage_persist_contains_write_failure: throwing writeConfigFn caught, returns false", () => {
    const throwing = jest.fn(() => {
      throw new Error("EPERM: config locked");
    });
    let result;
    expect(() => {
      result = dbmode.applyStoragePersist({}, { persistLocal: true }, throwing);
    }).not.toThrow();
    expect(result).toBe(false);
    expect(throwing).toHaveBeenCalledTimes(1);
  });
});

// ── restart-app: full relaunch + preload bridge ───────────────────────────────

describe("fullRelaunch", () => {
  test("test_restart_app_full_relaunch: calls app.relaunch() then app.quit(), in that order", () => {
    const calls = [];
    const appRef = {
      relaunch: () => calls.push("relaunch"),
      quit: () => calls.push("quit"),
    };
    restart.fullRelaunch(appRef);
    expect(calls).toEqual(["relaunch", "quit"]);
  });
});

describe("preload restart-app bridge", () => {
  test("test_preload_exposes_restart_app: restartApp() sends the restart-app IPC message", () => {
    ipcSent.length = 0;
    exposed = null;
    jest.isolateModules(() => {
      require("../preload");
    });
    expect(exposed).not.toBeNull();
    expect(exposed.name).toBe("celerp");
    expect(typeof exposed.api.restartApp).toBe("function");
    exposed.api.restartApp();
    expect(ipcSent).toContainEqual(["restart-app"]);
  });
});
