// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Waiting for the API to accept connections. Pure functions, no Electron imports,
// so it is safe to require in Jest.
//
// The first start after an update can rebuild every record from the ledger before
// the API listens (celerp/services/dev_release_guard.py), which takes minutes on a
// large company. So the wait lasts as long as the API process is alive, up to an
// upper limit, and ends early only when the process has stopped.

"use strict";

// The line celerp/projections/engine.py logs during a rebuild (the Python side is
// authoritative for its wording).
const PROGRESS = /Rebuilding records: (\d+) of (\d+) ledger entries/g;

/** The last rebuild progress in a chunk of API output, or null when it has none. */
function rebuildProgress(text) {
  let last = null;
  for (const m of String(text).matchAll(PROGRESS)) last = { done: Number(m[1]), total: Number(m[2]) };
  return last;
}

/**
 * Resolve once `isOpen()` is true. Reject when `hasExited()` turns true, or after
 * `maxMs`. `onSlow` is called once when the wait passes `slowAfterMs`.
 */
function waitForApi({ isOpen, hasExited, intervalMs = 500, maxMs = 30 * 60 * 1000,
                      slowAfterMs = 30 * 1000, onSlow = () => {} }) {
  const started = Date.now();
  let slowShown = false;
  return new Promise((resolve, reject) => {
    const check = async () => {
      if (await isOpen()) return resolve();
      if (hasExited()) return reject(new Error("The API stopped before it was ready"));
      const waited = Date.now() - started;
      if (waited >= maxMs) return reject(new Error(`The API did not start within ${Math.round(maxMs / 60000)} minutes`));
      if (!slowShown && waited >= slowAfterMs) { slowShown = true; onSlow(); }
      setTimeout(check, intervalMs);
    };
    check();
  });
}

module.exports = { waitForApi, rebuildProgress };
