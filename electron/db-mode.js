// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Pure functions extracted from app-main.js for unit testing.
// No Electron imports - safe to require in Jest.

"use strict";

/**
 * Returns true if the SaaS grace period is still active.
 *
 * @param {object} flags - feature_flags object (may be empty)
 * @returns {boolean}
 */
function isInGrace(flags) {
  return flags.grace_period_ends
    ? new Date(flags.grace_period_ends) > new Date()
    : false;
}

/**
 * Pure database-mode decision from config + feature flags. It never receives
 * dbPort and never returns a connection string: app-main.js alone owns the
 * bundled-URL build, so this module cannot fabricate one.
 *
 * @param {object} cfg - { db_mode, external_db_url, feature_flags }
 * @returns {{ startExternal: boolean, persistLocal: boolean, inGrace: boolean, gracePeriod: boolean }}
 */
function dbModeDecision(cfg) {
  const flags = cfg.feature_flags || {};
  const inGrace = isInGrace(flags);
  const externalAllowed = (flags.external_db || inGrace) && cfg.external_db_url;
  const startExternal = externalAllowed && cfg.db_mode === "external";
  const gracePeriod = startExternal && inGrace && !flags.external_db;
  const persistLocal = cfg.db_mode === "external" && !externalAllowed;
  return { startExternal, persistLocal, inGrace, gracePeriod };
}

/**
 * Persist db_mode=local at grace expiry through the injected writeConfigFn.
 * Patches only db_mode; external_db_url is never in the patch (#23). A write
 * failure is contained here and reported as a false return so a failed persist
 * never aborts boot; app-main.js's writeConfig logs the error before it rethrows.
 *
 * @param {object} cfg
 * @param {{ persistLocal: boolean }} decision
 * @param {(patch: object) => void} writeConfigFn - injected for testability
 * @returns {boolean} whether db_mode=local was persisted
 */
function applyDbModePersist(cfg, decision, writeConfigFn) {
  if (!decision.persistLocal) {
    return false;
  }
  try {
    writeConfigFn({ db_mode: "local" });
    return true;
  } catch (_e) {
    return false;
  }
}

/**
 * Pure external-storage decision from config + feature flags, mirroring
 * dbModeDecision. storageAllowed is the single storage-entitlement rule
 * (entitled or in grace, with S3 chosen); resolveStorageEnv consumes this so the
 * rule lives in one place.
 *
 * @param {object} cfg - { storage_mode, storage_s3_*, feature_flags }
 * @returns {{ startS3: boolean, persistLocal: boolean, inGrace: boolean, gracePeriod: boolean }}
 */
function storageModeDecision(cfg) {
  const flags = cfg.feature_flags || {};
  const inGrace = isInGrace(flags);
  const storageAllowed =
    (flags.external_storage || inGrace) && cfg.storage_mode === "s3";
  const startS3 = storageAllowed;
  const gracePeriod = startS3 && inGrace && !flags.external_storage;
  const persistLocal = cfg.storage_mode === "s3" && !storageAllowed;
  return { startS3, persistLocal, inGrace, gracePeriod };
}

/**
 * Persist storage_mode=local at grace expiry through the injected writeConfigFn.
 * Patches only storage_mode; the storage_s3_* settings are never in the patch so
 * the customer's endpoint/bucket/keys survive the fallback and can be reselected.
 * A write failure is contained to a false return so a failed persist never aborts
 * boot.
 *
 * @param {object} cfg
 * @param {{ persistLocal: boolean }} decision
 * @param {(patch: object) => void} writeConfigFn - injected for testability
 * @returns {boolean} whether storage_mode=local was persisted
 */
function applyStoragePersist(cfg, decision, writeConfigFn) {
  if (!decision.persistLocal) {
    return false;
  }
  try {
    writeConfigFn({ storage_mode: "local" });
    return true;
  } catch (_e) {
    return false;
  }
}

// Tri-state exit codes from celerp/entitlement_preflight.py. Kept here so the
// pure gate mapping and its jest tests do not depend on the spawn wiring.
const PREFLIGHT_RENEWED = 0;
const PREFLIGHT_EXPIRED = 2;
const PREFLIGHT_UNREACHABLE = 3;

/**
 * Map a preflight result onto the boot action, purely. The gate applies when
 * EITHER external resource would fall back to local AND there is a configured
 * target worth preserving: the database's external_db_url, or S3's endpoint. A
 * lapse of either resource must trigger the refresh/dialog, so an S3-only lapse
 * is no longer silently persisted while the database drives the exit code
 * alone. Otherwise the ordinary flow runs unchanged. An unknown exit code is
 * treated as UNREACHABLE so a packaging bug asks rather than silently switching
 * a resource.
 *
 * @param {object} cfg - { external_db_url, storage_s3_endpoint }
 * @param {{ persistLocal: boolean }} dbDecision
 * @param {{ persistLocal: boolean }} storageDecision
 * @param {number} exitCode - a preflight exit code
 * @returns {{ action: "none"|"external"|"fallback"|"confirm" }}
 */
function preflightGate(cfg, dbDecision, storageDecision, exitCode) {
  const dbFallsBack = dbDecision.persistLocal && Boolean(cfg.external_db_url);
  const storageFallsBack =
    storageDecision.persistLocal && Boolean(cfg.storage_s3_endpoint);
  if (!dbFallsBack && !storageFallsBack) {
    return { action: "none" };
  }
  if (exitCode === PREFLIGHT_RENEWED) return { action: "external" };
  if (exitCode === PREFLIGHT_EXPIRED) return { action: "fallback" };
  return { action: "confirm" };
}

module.exports = {
  isInGrace,
  dbModeDecision,
  applyDbModePersist,
  storageModeDecision,
  applyStoragePersist,
  preflightGate,
  PREFLIGHT_RENEWED,
  PREFLIGHT_EXPIRED,
  PREFLIGHT_UNREACHABLE,
};
