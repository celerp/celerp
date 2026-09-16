// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const {
  isInGrace,
  dbModeDecision,
  applyDbModePersist,
  storageModeDecision,
  applyStoragePersist,
  preflightGate,
  affectedResources,
} = require("../db-mode");

const DAY_MS = 24 * 60 * 60 * 1000;
const future = () => new Date(Date.now() + DAY_MS).toISOString();
const past = () => new Date(Date.now() - DAY_MS).toISOString();
const EXT_URL = "postgresql+asyncpg://celerp:secret@db.example.com:5432/celerp";

// ── isInGrace ─────────────────────────────────────────────────────────────────

describe("isInGrace", () => {
  test("true when grace_period_ends is in the future", () => {
    expect(isInGrace({ grace_period_ends: future() })).toBe(true);
  });

  test("false when grace_period_ends is in the past", () => {
    expect(isInGrace({ grace_period_ends: past() })).toBe(false);
  });

  test("test_isgrace_handles_invalid_dates: null and garbage return false", () => {
    expect(isInGrace({ grace_period_ends: null })).toBe(false);
    expect(isInGrace({})).toBe(false);
    expect(isInGrace({ grace_period_ends: "not-a-date" })).toBe(false);
  });
});

// ── dbModeDecision ────────────────────────────────────────────────────────────

describe("dbModeDecision", () => {
  test("test_dbmode_unpaid_team_starts_local: no entitlement, no url -> local", () => {
    const cfg = {
      db_mode: "local",
      external_db_url: "",
      feature_flags: { external_db: false, external_storage: false, grace_period_ends: null },
    };
    const d = dbModeDecision(cfg);
    expect(d.startExternal).toBe(false);
    expect(d.persistLocal).toBe(false);
  });

  test("test_dbmode_active_team_starts_external: entitled + external configured -> external", () => {
    const cfg = {
      db_mode: "external",
      external_db_url: EXT_URL,
      feature_flags: { external_db: true, external_storage: false, grace_period_ends: null },
    };
    const d = dbModeDecision(cfg);
    expect(d.startExternal).toBe(true);
    expect(d.gracePeriod).toBe(false);
    expect(d.persistLocal).toBe(false);
  });

  test("test_dbmode_grace_starts_external: in grace + external configured -> external, gracePeriod", () => {
    const cfg = {
      db_mode: "external",
      external_db_url: EXT_URL,
      feature_flags: { external_db: false, external_storage: false, grace_period_ends: future() },
    };
    const d = dbModeDecision(cfg);
    expect(d.startExternal).toBe(true);
    expect(d.gracePeriod).toBe(true);
    expect(d.persistLocal).toBe(false);
  });

  test("test_dbmode_expiry_starts_local: grace expired + external configured -> local, persistLocal", () => {
    const cfg = {
      db_mode: "external",
      external_db_url: EXT_URL,
      feature_flags: { external_db: false, external_storage: false, grace_period_ends: past() },
    };
    const d = dbModeDecision(cfg);
    expect(d.startExternal).toBe(false);
    expect(d.persistLocal).toBe(true);
  });

  test("test_dbmode_renewal_stays_local: entitlement restored but db_mode local -> stays local", () => {
    const cfg = {
      db_mode: "local",
      external_db_url: EXT_URL,
      feature_flags: { external_db: true, external_storage: false, grace_period_ends: null },
    };
    const d = dbModeDecision(cfg);
    expect(d.startExternal).toBe(false);
    expect(d.persistLocal).toBe(false);
  });

  test("test_dbmode_decision_returns_no_url: decision never carries a connection string", () => {
    const cfg = {
      db_mode: "external",
      external_db_url: EXT_URL,
      feature_flags: { external_db: true, external_storage: false, grace_period_ends: null },
    };
    expect(dbModeDecision(cfg)).not.toHaveProperty("url");
  });

  test("test_dbmode_persist_idempotent_across_reboots: local + not entitled + not grace -> no persist", () => {
    const cfg = {
      db_mode: "local",
      external_db_url: "",
      feature_flags: { external_db: false, external_storage: false, grace_period_ends: null },
    };
    expect(dbModeDecision(cfg).persistLocal).toBe(false);
  });
});

// ── applyDbModePersist ────────────────────────────────────────────────────────

describe("applyDbModePersist", () => {
  test("test_dbmode_persist_writes_local_only: writes exactly {db_mode:'local'} once (external_db_url untouched)", () => {
    const writeConfigFn = jest.fn();
    const cfg = { db_mode: "external", external_db_url: EXT_URL };
    const persisted = applyDbModePersist(cfg, { persistLocal: true }, writeConfigFn);
    expect(persisted).toBe(true);
    expect(writeConfigFn).toHaveBeenCalledTimes(1);
    expect(writeConfigFn).toHaveBeenCalledWith({ db_mode: "local" });
    const patch = writeConfigFn.mock.calls[0][0];
    expect(Object.keys(patch)).toEqual(["db_mode"]);
    expect(patch).not.toHaveProperty("external_db_url");
  });

  test("test_dbmode_no_persist_when_active: no write when active or in grace", () => {
    const writeConfigFn = jest.fn();
    expect(applyDbModePersist({}, { persistLocal: false }, writeConfigFn)).toBe(false);
    expect(writeConfigFn).not.toHaveBeenCalled();
  });

  test("test_dbmode_persist_survives_write_error: throwing writeConfigFn is caught, no throw", () => {
    const throwing = jest.fn(() => { throw new Error("EPERM: config locked"); });
    let result;
    expect(() => { result = applyDbModePersist({}, { persistLocal: true }, throwing); }).not.toThrow();
    expect(result).toBe(false);
    expect(throwing).toHaveBeenCalledTimes(1);
  });
});

// ── preflightGate ─────────────────────────────────────────────────────────────
// Tri-state exit codes mirror celerp/entitlement_preflight.py: RENEWED 0,
// EXPIRED 2, UNREACHABLE 3.

describe("preflightGate", () => {
  const RENEWED = 0;
  const EXPIRED = 2;
  const UNREACHABLE = 3;

  const noFallback = { persistLocal: false };
  const dbFallback = { persistLocal: true };
  const storageFallback = { persistLocal: true };
  const S3_CFG = {
    external_db_url: "",
    storage_mode: "s3",
    storage_s3_endpoint: "https://s3.example.com",
  };

  test("no gate when neither resource falls back to local", () => {
    expect(preflightGate({ external_db_url: EXT_URL }, noFallback, noFallback, EXPIRED))
      .toEqual({ action: "none" });
  });

  test("no gate when db falls back but no external_db_url is on file", () => {
    expect(preflightGate({ external_db_url: "" }, dbFallback, noFallback, EXPIRED))
      .toEqual({ action: "none" });
  });

  test("RENEWED continues external and does not persist local (db)", () => {
    expect(preflightGate({ external_db_url: EXT_URL }, dbFallback, noFallback, RENEWED))
      .toEqual({ action: "external" });
  });

  test("EXPIRED falls back to local (db)", () => {
    expect(preflightGate({ external_db_url: EXT_URL }, dbFallback, noFallback, EXPIRED))
      .toEqual({ action: "fallback" });
  });

  test("UNREACHABLE asks for confirmation, never a silent switch (db)", () => {
    expect(preflightGate({ external_db_url: EXT_URL }, dbFallback, noFallback, UNREACHABLE))
      .toEqual({ action: "confirm" });
  });

  test("an unknown exit code is treated as UNREACHABLE (confirm)", () => {
    expect(preflightGate({ external_db_url: EXT_URL }, dbFallback, noFallback, 1))
      .toEqual({ action: "confirm" });
  });

  // check_js_storage_mode_decision_gated: the gate triggers on an S3-only
  // fallback too, not only the database. Without this an S3-only lapse would
  // skip the refresh/dialog and silently persist storage_mode=local.
  test("check_js_storage_mode_decision_gated: an S3-only fallback triggers the gate", () => {
    expect(preflightGate(S3_CFG, noFallback, storageFallback, EXPIRED))
      .toEqual({ action: "fallback" });
    expect(preflightGate(S3_CFG, noFallback, storageFallback, UNREACHABLE))
      .toEqual({ action: "confirm" });
    expect(preflightGate(S3_CFG, noFallback, storageFallback, RENEWED))
      .toEqual({ action: "external" });
  });

  test("no gate when storage falls back but no S3 endpoint is configured", () => {
    const cfg = { external_db_url: "", storage_mode: "s3", storage_s3_endpoint: "" };
    expect(preflightGate(cfg, noFallback, storageFallback, EXPIRED))
      .toEqual({ action: "none" });
  });

  test("either resource forcing local triggers the gate (db and S3 both lapsed)", () => {
    const cfg = {
      external_db_url: EXT_URL,
      storage_mode: "s3",
      storage_s3_endpoint: "https://s3.example.com",
    };
    expect(preflightGate(cfg, dbFallback, storageFallback, EXPIRED))
      .toEqual({ action: "fallback" });
  });
});

// A2: applyStoragePersist must not persist storage_mode=local off a stale
// decision before the trigger resolves. These pin the pure helper's contract
// that the persist writes exactly {storage_mode:'local'} and only when the
// re-read decision still forces local.
describe("applyStoragePersist storage fallback", () => {
  test("test_storage_persist_writes_local_only: writes exactly {storage_mode:'local'}", () => {
    const writeConfigFn = jest.fn();
    const persisted = applyStoragePersist(
      { storage_mode: "s3" }, { persistLocal: true }, writeConfigFn);
    expect(persisted).toBe(true);
    expect(writeConfigFn).toHaveBeenCalledTimes(1);
    const patch = writeConfigFn.mock.calls[0][0];
    expect(Object.keys(patch)).toEqual(["storage_mode"]);
    expect(patch).toEqual({ storage_mode: "local" });
  });

  test("no persist when storage entitlement is still active", () => {
    const writeConfigFn = jest.fn();
    expect(applyStoragePersist({}, { persistLocal: false }, writeConfigFn)).toBe(false);
    expect(writeConfigFn).not.toHaveBeenCalled();
  });
});

describe("affectedResources", () => {
  // check_js_preflight_dialog_names_all_resources: when both the database and
  // S3 lapse, the unreachable-check dialog names both, so the user cannot miss
  // that both will diverge under a local fallback.
  test("check_js_preflight_dialog_names_all_resources: both lapsed names both", () => {
    expect(affectedResources(true, true))
      .toBe("external database and external file storage");
  });

  test("only the database lapsed names the database alone", () => {
    expect(affectedResources(true, false)).toBe("external database");
  });

  test("only S3 lapsed names external file storage alone", () => {
    expect(affectedResources(false, true)).toBe("external file storage");
  });
});
