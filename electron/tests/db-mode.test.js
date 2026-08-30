// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const { isInGrace, dbModeDecision, applyDbModePersist } = require("../db-mode");

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
