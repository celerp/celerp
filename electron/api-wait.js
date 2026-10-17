// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Waiting for the API to accept connections. Pure functions, no Electron imports,
// so it is safe to require in Jest.
//
// The first start after an update can rebuild every record from the ledger before
// the API listens (celerp/services/dev_release_guard.py), which takes minutes, or on
// a very large company longer. So the wait has no fixed end: it lasts while the API
// process is alive and still reporting rebuild progress, and ends early only when the
// process stops or goes quiet for too long.

"use strict";

// The line celerp/projections/engine.py logs during a rebuild (the Python side is
// authoritative for its wording).
const PROGRESS = /Rebuilding records: (\d+) of (\d+) ledger entries/;

/**
 * The rebuild progress in one line of API output, or null when it has none. An empty
 * ledger (a fresh install) has nothing to rebuild, so its "0 of 0" is not progress.
 */
function rebuildProgress(line) {
  const m = PROGRESS.exec(String(line));
  return m && Number(m[2]) > 0 ? { done: Number(m[1]), total: Number(m[2]) } : null;
}

// Longest partial line kept between chunks. Log lines are far shorter; this only
// bounds memory if a process ever writes without newlines.
const MAX_PARTIAL = 64 * 1024;

/**
 * Turn a stream's chunks into whole lines. A line split across two chunks is joined
 * before `onLine` sees it. One splitter per stream, so stdout and stderr never mix.
 */
function lineSplitter(onLine) {
  let partial = "";
  return (chunk) => {
    const lines = (partial + String(chunk)).split(/\r?\n/);
    partial = lines.pop().slice(-MAX_PARTIAL);
    for (const line of lines) onLine(line);
  };
}

/**
 * Resolve once `isOpen()` is true. Reject when `hasExited()` turns true, or when
 * `quietMs` passes with no sign of life: the wait starts the clock, and every
 * `lastProgressAt()` later than that restarts it. `onSlow` is called once when the
 * wait passes `slowAfterMs`.
 */
function waitForApi({ isOpen, hasExited, lastProgressAt = () => 0, intervalMs = 500,
                      quietMs = 30 * 60 * 1000, slowAfterMs = 30 * 1000, onSlow = () => {} }) {
  const started = Date.now();
  let slowShown = false;
  return new Promise((resolve, reject) => {
    const check = async () => {
      if (await isOpen()) return resolve();
      if (hasExited()) return reject(new Error("The API stopped before it was ready"));
      const now = Date.now();
      if (now - Math.max(started, lastProgressAt()) >= quietMs) {
        return reject(new Error(`The API made no progress for ${Math.round(quietMs / 60000)} minutes`));
      }
      if (!slowShown && now - started >= slowAfterMs) { slowShown = true; onSlow(); }
      setTimeout(check, intervalMs);
    };
    check();
  });
}

/**
 * The last `max` characters of a server's output, for the error shown when it fails
 * to start. Bounded because a server writes for as long as it runs; its log file
 * keeps everything.
 */
function outputTail(max = 3000) {
  let text = "";
  return { add: (s) => { text = (text + s).slice(-max); }, text: () => text };
}

module.exports = { waitForApi, rebuildProgress, lineSplitter, outputTail };
