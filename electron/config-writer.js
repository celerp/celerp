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
const path = require("path");
const crypto = require("crypto");

/**
 * Fsync the containing directory so a preceding atomic rename is durable, not
 * only the renamed file's contents. No-op on platforms that cannot fsync a
 * directory (Windows rejects opening a directory for fsync), and any error is
 * swallowed because the rename itself already committed the write. Mirrors
 * celerp/config_store.py's _fsync_dir.
 */
function fsyncDir(dirPath) {
  if (process.platform === "win32") return;
  let dirFd;
  try {
    dirFd = fs.openSync(dirPath, "r");
  } catch {
    return;
  }
  try {
    fs.fsyncSync(dirFd);
  } catch {
    // Filesystem or platform rejected the directory fsync; the rename stands.
  } finally {
    try {
      fs.closeSync(dirFd);
    } catch {
      // Already closed.
    }
  }
}

// Lock protocol constants, identical to celerp/config_store.py. Overridable per
// call (opts) only so the tests can shrink the budget; production uses these.
const LOCK_BUDGET_MS = 5000;
const LOCK_RETRY_MS = 50;
const LOCK_STALE_MS = 10000;

// A unique owner token for one acquisition (pid plus a random suffix), written
// as the whole lock-file body and re-read for control, identical in shape to
// the Python writer's f"{pid} {uuid4().hex}".
function lockToken() {
  return `${process.pid} ${crypto.randomBytes(16).toString("hex")}`;
}

// Return the current lock-file body, or null when it cannot be read.
function readLockToken(lockPath) {
  try {
    return fs.readFileSync(lockPath, "utf8");
  } catch {
    return null;
  }
}

/**
 * Block the current thread for `ms` without a busy spin. The boot-path writers
 * are synchronous, so a real blocking sleep (Atomics.wait on a throwaway
 * SharedArrayBuffer) is used rather than an async timer.
 */
function sleep(ms) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

/**
 * Acquire the config lock by exclusive create, returning { fd, token }. The
 * token is the exact lock-file body written; the caller passes it back to
 * releaseLock so only the electing writer ever removes this lock. Throws when
 * another writer holds a fresh lock past the budget. A lock whose mtime has aged
 * past the stale threshold is token-verified before removal: the stale token is
 * re-read immediately before the unlink, so a faster successor that already
 * replaced the lock is never deleted, after which the exclusive create stays the
 * only way to win the takeover race.
 */
function acquireLock(lockPath, opts) {
  const budgetMs = opts.budgetMs ?? LOCK_BUDGET_MS;
  const retryMs = opts.retryMs ?? LOCK_RETRY_MS;
  const staleMs = opts.staleMs ?? LOCK_STALE_MS;
  const deadline = Date.now() + budgetMs;
  for (;;) {
    try {
      const fd = fs.openSync(lockPath, "wx", 0o600);
      const token = lockToken();
      fs.writeSync(fd, token);
      fs.fsyncSync(fd);
      return { fd, token };
    } catch (err) {
      if (err.code !== "EEXIST") throw err;
      let age = 0;
      try {
        age = Date.now() - fs.statSync(lockPath).mtimeMs;
      } catch {
        age = 0;
      }
      if (age > staleMs) {
        // Re-read the stale token immediately before removing and unlink only
        // that same token, so a faster successor's lock is never deleted.
        const staleToken = readLockToken(lockPath);
        if (staleToken !== null) {
          try {
            if (readLockToken(lockPath) === staleToken) {
              fs.unlinkSync(lockPath);
            }
          } catch {
            // Raced with another taker; loop and retry the exclusive create.
          }
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

/**
 * Release the lock, unlinking it only while it still holds this owner's token;
 * a lock a stale takeover already replaced is left untouched, so a superseded
 * owner's release is a no-op.
 */
function releaseLock(fd, lockPath, token) {
  try {
    fs.closeSync(fd);
  } catch {
    // Already closed.
  }
  try {
    if (readLockToken(lockPath) === token) {
      fs.unlinkSync(lockPath);
    }
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
  const { fd, token } = acquireLock(lockPath, opts);
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
    fsyncDir(path.dirname(configPath));
  } catch (err) {
    try {
      fs.unlinkSync(tmp);
    } catch {
      // Temp may not exist if the failure was before its creation.
    }
    throw err;
  } finally {
    releaseLock(fd, lockPath, token);
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

module.exports = { writeConfig, acquireLock, releaseLock };
