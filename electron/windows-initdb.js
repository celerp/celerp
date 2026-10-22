// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The first database setup on Windows either completes or leaves nothing behind.
// initdb writes into a separate setup folder next to the data folder, which is
// moved into place only when initdb succeeds. A setup cut short (the app closed,
// the PC switched off) leaves only the setup folder, which the next start clears
// and runs again. Only a setup folder this code made is ever deleted: its identity
// is recorded in an owner file before initdb writes anything into it, and a lock
// file keeps two starts from setting up at the same time. A setup folder without
// a matching owner file, or a data folder that holds files but no cluster, stops
// the start with a message saying what to do.
"use strict";

const fs = require("fs");
const path = require("path");

// A lock older than this is not trusted even when its process ID is in use: the
// ID may belong to an unrelated process started since. A real setup takes seconds.
const LOCK_STALE_MS = 10 * 60 * 1000;
// How long a move into place waits for Windows to let go of the new folder.
const HELD_WAIT_MS = 60 * 1000;
const HELD = new Set(["EBUSY", "EPERM", "EACCES"]);

function processIsRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

/** The identity of a folder: the same folder keeps it, a recreated one does not. */
function folderIdentity(dir) {
  const st = fs.statSync(dir, { bigint: true });
  return `${st.dev}:${st.ino}:${st.birthtimeNs}`;
}

/** Write `body` to `file` and flush it to disk before returning. */
function writeDurably(file, body, flag = "w") {
  const fd = fs.openSync(file, flag);
  try {
    fs.writeSync(fd, body);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
}

function busy(dataDir) {
  return new Error(
    `Another Celerp start is setting up the database in ${dataDir}. Wait for it to ` +
    "finish, then start Celerp again.");
}

/** Take the setup lock, or throw when another live start holds it. */
function takeLock(lockFile, dataDir, isRunning, now) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      writeDurably(lockFile, `${process.pid}\n`, "wx");
      return;
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
    }
    let pid = NaN;
    let ageMs = 0;
    try {
      pid = parseInt(fs.readFileSync(lockFile, "utf8"), 10);
      ageMs = now() - fs.statSync(lockFile).mtimeMs;
    } catch (e) {
      if (e.code === "ENOENT") continue; // released a moment ago
      throw e;
    }
    // A half-written lock (no number yet) is treated as held until it ages out.
    const held = ageMs < LOCK_STALE_MS && (Number.isNaN(pid) || (pid !== process.pid && isRunning(pid)));
    if (held) throw busy(dataDir);
    fs.rmSync(lockFile, { force: true });
  }
  throw busy(dataDir);
}

/**
 * Remove a setup folder left by an earlier start, but only when it is the folder
 * this code made (its identity matches the owner file) or it is empty. Anything
 * else is someone's folder and stops the start.
 */
function clearOwnStaging(staging, ownerFile) {
  if (!fs.existsSync(staging)) return;
  let recorded = "";
  try {
    recorded = fs.readFileSync(ownerFile, "utf8").trim();
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  if (recorded && recorded === folderIdentity(staging)) {
    fs.rmSync(staging, { recursive: true, force: true });
    return;
  }
  if (fs.statSync(staging).isDirectory() && fs.readdirSync(staging).length === 0) {
    fs.rmdirSync(staging);
    return;
  }
  throw new Error(
    `Celerp found ${staging}, which it did not create. Move it somewhere else (keep ` +
    "it if it may hold data), then start Celerp again.");
}

/**
 * Windows keeps a new folder busy for a moment after the process that wrote it exits
 * (its last child, an antivirus scan), so the move is retried while the folder is held,
 * up to HELD_WAIT_MS. Any other failure is thrown at once.
 */
async function renameWhenReleased(from, to, now, sleep) {
  const deadline = now() + HELD_WAIT_MS;
  for (;;) {
    try {
      fs.renameSync(from, to);
      return;
    } catch (error) {
      if (!HELD.has(error.code) || now() >= deadline) throw error;
      await sleep(500);
    }
  }
}

/**
 * Create the cluster in `dataDir` unless it already has one. `runInitdb(target)`
 * runs initdb into `target` (an existing empty folder) and rejects on failure.
 * Resolves true when a cluster was created, false when one was already there.
 * `isRunning(pid)`, `now()` and `sleep(ms)` are injectable for tests.
 */
async function initialiseClusterOnce({ dataDir, runInitdb, isRunning = processIsRunning, now = Date.now,
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)) }) {
  if (fs.existsSync(path.join(dataDir, "PG_VERSION"))) return false;
  const staging = `${dataDir}.init`;
  const ownerFile = `${staging}.owner`;
  const lockFile = `${staging}.lock`;
  fs.mkdirSync(path.dirname(dataDir), { recursive: true });
  takeLock(lockFile, dataDir, isRunning, now);
  try {
    // Another start may have finished the setup while this one waited for the lock.
    if (fs.existsSync(path.join(dataDir, "PG_VERSION"))) return false;
    const existing = fs.existsSync(dataDir) ? fs.readdirSync(dataDir) : [];
    if (existing.length > 0) {
      throw new Error(
        `The database folder ${dataDir} holds files but no database. Move that folder ` +
        "somewhere else (keep it if it may hold data), then start Celerp again.");
    }
    clearOwnStaging(staging, ownerFile);
    // The folder is created here and its identity recorded before initdb writes
    // into it, so a folder found later is deleted only if it is this one.
    fs.mkdirSync(staging);
    let created = false;
    try {
      writeDurably(ownerFile, `${folderIdentity(staging)}\n`);
      await runInitdb(staging);
      // Windows cannot rename onto an existing folder; this one is empty (checked above).
      if (fs.existsSync(dataDir)) fs.rmdirSync(dataDir);
      await renameWhenReleased(staging, dataDir, now, sleep);
      created = true;
    } finally {
      if (!created) fs.rmSync(staging, { recursive: true, force: true });
      fs.rmSync(ownerFile, { force: true });
    }
    return true;
  } finally {
    fs.rmSync(lockFile, { force: true });
  }
}

// Postgres on Windows reads its own path, its data folder and the paths it is
// given in the ANSI code page, so letters outside it (an account named in Thai or
// Chinese, whose user folder holds a per-user install and every data folder) turn
// into "?" and setup or start fails. A path with letters outside ASCII is replaced
// by one in an ASCII folder private to the user: the program folders by links to
// them, a new database by a folder there. A database that already exists stays
// where it is.
const ASCII_PATH = /^[\x20-\x7e]+$/;

/** Point the link at `linkPath` to `target`, creating or repointing it. */
function linkTo(linkPath, target) {
  let found = null;
  try {
    found = fs.lstatSync(linkPath);
  } catch (e) {
    if (e.code !== "ENOENT") throw e;
  }
  // Anything at the path other than a link is not this code's to replace.
  if (found && !found.isSymbolicLink()) {
    throw new Error(
      `Celerp found ${linkPath}, which it did not create. Move it somewhere else (keep ` +
      "it if it may hold data), then start Celerp again.");
  }
  if (found) {
    if (path.resolve(fs.readlinkSync(linkPath)) === path.resolve(target)) return linkPath;
    fs.unlinkSync(linkPath);
  }
  fs.symlinkSync(target, linkPath, "junction");
  return linkPath;
}

/**
 * The folders Postgres is run with: `nativeDir` (the server programs and their
 * input files), `clientBinDir` (the backup tools) and `dataDir`, each as given
 * when Postgres can read it, plus `privateRoot`: the user's private ASCII folder
 * when one is used, else null. `privateRoot()` returns that folder, creating it;
 * it is called only when a path needs replacing.
 */
function postgresPaths({ dataDir, nativeDir, clientBinDir, privateRoot }) {
  let root = null;
  const inRoot = (name) => {
    if (root === null) {
      root = privateRoot();
      if (!ASCII_PATH.test(root)) throw new Error(`The folder ${root} for the database has letters outside ASCII.`);
    }
    return path.join(root, name);
  };
  const usable = (p) => ASCII_PATH.test(p);
  const paths = {
    nativeDir: usable(nativeDir) ? nativeDir : linkTo(inRoot("native"), nativeDir),
    clientBinDir: usable(clientBinDir) ? clientBinDir : linkTo(inRoot("pg-bin"), clientBinDir),
    dataDir: usable(dataDir) || fs.existsSync(path.join(dataDir, "PG_VERSION")) ? dataDir : inRoot("postgres"),
  };
  return { ...paths, privateRoot: root };
}

module.exports = { initialiseClusterOnce, postgresPaths };
