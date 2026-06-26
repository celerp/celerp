// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const { migrateArgs } = require("../migrate_cmd");

describe("migrateArgs", () => {
  test("routes through the celerp CLI, not raw alembic", () => {
    const args = migrateArgs("postgresql+asyncpg://celerp:celerp@localhost:5432/celerp");
    expect(args).toEqual([
      "-m", "celerp", "migrate",
      "--db-url", "postgresql+asyncpg://celerp:celerp@localhost:5432/celerp",
    ]);
    // The whole point of the change: the launcher must NOT run raw alembic,
    // which crashes on a develop-origin (create_all) database.
    expect(args).not.toContain("alembic");
  });

  test("passes the database URL through verbatim", () => {
    expect(migrateArgs("postgresql://x/y")).toContain("postgresql://x/y");
  });
});
