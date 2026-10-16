// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The first database setup on Windows either completes or leaves nothing behind.
// initdb writes into a separate setup folder next to the data folder, which is
// moved into place only when initdb succeeds. A setup cut short (the app closed,
// the PC switched off) leaves only the setup folder, which the next start clears
// and runs again. The data folder itself is never deleted: one that holds files
// but no cluster stops the start with a message saying what to do.
"use strict";

const fs = require("fs");
const path = require("path");

/**
 * Create the cluster in `dataDir` unless it already has one. `runInitdb(target)`
 * runs initdb into `target` and rejects on failure. Resolves true when a cluster
 * was created, false when one was already there.
 */
async function initialiseClusterOnce({ dataDir, runInitdb }) {
  if (fs.existsSync(path.join(dataDir, "PG_VERSION"))) return false;
  const existing = fs.existsSync(dataDir) ? fs.readdirSync(dataDir) : [];
  if (existing.length > 0) {
    throw new Error(
      `The database folder ${dataDir} holds files but no database. Move that folder ` +
      "somewhere else (keep it if it may hold data), then start Celerp again.");
  }
  const staging = `${dataDir}.init`;
  fs.rmSync(staging, { recursive: true, force: true });
  try {
    await runInitdb(staging);
    // Windows cannot rename onto an existing folder; this one is empty (checked above).
    if (fs.existsSync(dataDir)) fs.rmdirSync(dataDir);
    fs.renameSync(staging, dataDir);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
  return true;
}

module.exports = { initialiseClusterOnce };
