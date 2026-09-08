// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Locked writer for the packaged celerp-config.json. No Electron imports, so it
// is safe to require in Jest and to run as a standalone CLI. It shares one
// cross-process lock protocol with celerp/config_store.py: an O_EXCL-created
// sidecar lock file, retried up to a budget, with a stale-mtime takeover, so a
// Python writer and this Node writer never race on the same config file.

"use strict";

const fs = require("fs");
const crypto = require("crypto");

// Lock protocol constants, identical to celerp/config_store.py. Overridable per
// call (opts) only so the tests can shrink the budget; production uses these.
const LOCK_BUDGET_MS = 5000;
const LOCK_RETRY_MS = 50;
const LOCK_STALE_MS = 10000;

/**
 * Block the current thread for `ms` without a busy spin. The boot-path writers
 * are synchronous, so a real blocking sleep (Atomics.wait on a throwaway
 * SharedArrayBuffer) is used rather than an async timer.
 */
function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/**
 * Acquire the config lock by exclusive create, returning the open fd. Throws
 * when another writer holds a fresh lock past the budget. A lock whose mtime
 * has aged past the stale threshold is unlinked, after which the exclusive
 * create stays the only way to win the takeover race.
 */
function acquireLock(lockPath, opts) {
  const budgetMs = opts.budgetMs ?? LOCK_BUDGET_MS;
  const retryMs = opts.retryMs ?? LOCK_RETRY_MS;
  const staleMs = opts.staleMs ?? LOCK_STALE_MS;
  const deadline = Date.now() + budgetMs;
  for (;;) {
    try {
      const fd = fs.openSync(lockPath, "wx", 0o600);
      try {
        fs.writeSync(fd, `${process.pid} ${Date.now()}`);
      } catch {
        // Diagnostics only; a failed write here never blocks acquisition.
      }
      return fd;
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
      let age = 0;
      try {
        age = Date.now() - fs.statSync(lockPath).mtimeMs;
      } catch {
        age = 0;
      }
      if (age > staleMs) {
        try {
          fs.unlinkSync(lockPath);
        } catch {
          // Raced with another taker; loop and retry the exclusive create.
        }
        continue;
      }
      if (Date.now() >= deadline) {
        throw new Error(`config-writer: lock ${lockPath} held beyond ${budgetMs}ms`);
      }
      sleep(retryMs);
    }
  }
}

function releaseLock(fd, lockPath) {
  try {
    fs.closeSync(fd);
  } catch {
    // Already closed.
  }
  try {
    fs.unlinkSync(lockPath);
  } catch {
    // Already gone via a stale takeover.
  }
}

function readRaw(configPath) {
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, "utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * Merge `patch` into the config at `configPath` in one atomic, locked write.
 * Re-reads the file inside the lock, merges, writes a unique 0600 temp, fsyncs,
 * and renames into place; the temp is removed and the error rethrown on
 * failure. The lock is always released. `opts` (budgetMs/retryMs/staleMs) is a
 * test seam only.
 */
function writeConfig(configPath, patch, opts = {}) {
  const lockPath = `${configPath}.lock`;
  const fd = acquireLock(lockPath, opts);
  const tmp = `${configPath}.${crypto.randomBytes(8).toString("hex")}.tmp`;
  try {
    const merged = { ...readRaw(configPath), ...patch };
    const out = fs.openSync(tmp, "wx", 0o600);
    try {
      fs.writeSync(out, JSON.stringify(merged, null, 2));
      fs.fsyncSync(out);
    } finally {
      fs.closeSync(out);
    }
    fs.renameSync(tmp, configPath);
  } catch (err) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      // Temp may not exist if the failure was before its creation.
    }
    throw err;
  } finally {
    releaseLock(fd, lockPath);
  }
}

// CLI entry: node config-writer.js <configPath> <key> <value>. A numeric value
// is stored as a Number, otherwise as the raw string. Used by the cross-process
// concurrency test to drive a real second writer.
if (require.main === module) {
  const [configPath, key, rawValue] = process.argv.slice(2);
  if (!configPath || key === undefined) {
    console.error("usage: config-writer.js <configPath> <key> <value>");
    process.exit(2);
  }
  const value =
    rawValue !== undefined && rawValue.trim() !== "" && !Number.isNaN(Number(rawValue))
      ? Number(rawValue)
      : rawValue;
  try {
    writeConfig(configPath, { [key]: value });
  } catch (err) {
    console.error(`config-writer: ${err.message}`);
    process.exit(1);
  }
}

module.exports = { writeConfig };
