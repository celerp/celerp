// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Stops the app's servers and then its database, once, before the app exits.
// No Electron imports, so it runs under Jest with real child processes.

"use strict";

function bounded(promise, ms, timedOut) {
  let timer;
  return Promise.race([promise, new Promise((resolve) => { timer = setTimeout(() => resolve(timedOut), ms); })])
    .finally(() => clearTimeout(timer));
}

// Resolves true once the child has exited: SIGTERM first, SIGKILL after graceMs,
// false if it still has not exited graceMs after that.
function stopChild(child, graceMs) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve(true);
  const exited = new Promise((resolve) => child.once("exit", () => resolve(true)));
  child.kill();
  return bounded(exited, graceMs, null).then((done) => {
    if (done) return true;
    child.kill("SIGKILL");
    return bounded(exited, graceMs, false);
  });
}

/**
 * @param {{
 *   children: () => Array<import("child_process").ChildProcess | null>,
 *   stopDatabase: () => Promise<void>,
 *   graceMs?: number,
 *   databaseMs?: number,
 *   log?: (message: string) => void,
 * }} deps
 */
function createShutdown({ children, stopDatabase, graceMs = 5000, databaseMs = 15000, log = console.error }) {
  let pending = null;
  let stopped = false;
  const restarts = new Set();

  async function run() {
    // A restart already under way finishes first, so the servers it starts are stopped too.
    await bounded(Promise.allSettled([...restarts]), databaseMs, null);
    const servers = await Promise.all(children().map((child) => stopChild(child, graceMs)));
    if (servers.includes(false)) log("[shutdown] a server did not exit");
    const database = await bounded(
      Promise.resolve().then(stopDatabase).then(() => true, (err) => { log(`[shutdown] database stop failed: ${err?.message ?? err}`); return true; }),
      databaseMs, false);
    if (!database) log("[shutdown] the database did not stop in time");
    stopped = true;
  }

  return {
    get stopping() { return pending !== null; },
    get stopped() { return stopped; },
    shutdown() {
      if (!pending) pending = run();
      return pending;
    },
    trackRestart(promise) {
      restarts.add(promise);
      promise.finally(() => restarts.delete(promise)).catch(() => {});
      return promise;
    },
  };
}

module.exports = { createShutdown, stopChild };
