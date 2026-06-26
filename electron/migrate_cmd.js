// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

// Argv for the bundled-Python database migration step.
//
// Routed through the celerp CLI (`python -m celerp migrate`) instead of a raw
// `python -m alembic upgrade head`. The CLI path auto-stamps a develop-origin
// (create_all) database before upgrading and then runs the develop→release
// reconcile (replay data backfills + projection rebuild on a version change),
// none of which raw alembic does — raw alembic crashes on such a database.
//
// Pure (no electron import) so it is unit-testable, like restart.js.
function migrateArgs(dbUrl) {
  return ["-m", "celerp", "migrate", "--db-url", dbUrl];
}

module.exports = { migrateArgs };
