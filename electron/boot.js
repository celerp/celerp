// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// The part of desktop startup that touches the data, in order.
//
// The database is checked before anything writes to the data directory, the
// config or the database: when this copy may not open it, none of the steps
// after the check run. Pure apart from the injected steps, so the order is
// unit-testable without Electron, like restart.js.

/**
 * Cold start: start the bundled database (when used), check it, then persist the
 * resolved modes, seed and set up modules, migrate, and start the servers.
 * Resolves false when the check refused and nothing after it ran.
 */
async function openData(steps) {
  if (steps.startPostgres) await steps.startPostgres();
  if (!(await steps.mayOpenData())) return false;
  steps.applyDbModePersist();
  steps.applyStoragePersist();
  steps.seedDefaultModules();
  steps.runModuleSetup();
  steps.runMigrations();
  await steps.startApi();
  await steps.startUi();
  return true;
}

/**
 * Server restart (after a module change or a restore replaced the database): check
 * the database again, then migrate and start the API. Resolves false when the check
 * refused and nothing after it ran.
 */
async function reopenData(steps) {
  if (!(await steps.mayOpenData())) return false;
  steps.runMigrations();
  await steps.startApi();
  return true;
}

module.exports = { openData, reopenData };
