// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// C1: unit-test the L0 matcher with fixtures (no filesystem, no build), including
// the negative control - the exact v1.2.6 mistake must be detected.
"use strict";

const { relativeRequires, isCovered } = require("./require-coverage.lib");

const ELECTRON = "/app/electron";
const MAIN = ELECTRON + "/main.js";

describe("relativeRequires", () => {
  test("finds relative requires, ignores builtins/modules", () => {
    const src = `
      const a = require("./restart");
      const b = require('../lib/x');
      const c = require("electron");
      const d = require("path");
    `;
    expect(relativeRequires(src)).toEqual(["./restart", "../lib/x"]);
  });
  test("no relative requires -> empty", () => {
    expect(relativeRequires(`const x = require("fs");`)).toEqual([]);
  });
});

describe("isCovered", () => {
  test("covered: ./migrate_cmd with migrate_cmd.js in files", () => {
    expect(isCovered("./migrate_cmd", MAIN, ELECTRON, ["main.js", "migrate_cmd.js"])).toBe(true);
  });

  // The v1.2.6 mistake, as a unit assertion: require added, allowlist not updated.
  test("NOT covered: ./migrate_cmd missing from files (the v1.2.6 bug)", () => {
    expect(isCovered("./migrate_cmd", MAIN, ELECTRON, ["main.js"])).toBe(false);
  });

  test("covered: extensionless resolves to .js", () => {
    expect(isCovered("./restart", MAIN, ELECTRON, ["restart.js"])).toBe(true);
  });
  test("covered: explicit .js specifier", () => {
    expect(isCovered("./app-main.js", MAIN, ELECTRON, ["app-main.js"])).toBe(true);
  });
  test("covered by /** glob", () => {
    expect(isCovered("./assets/x", MAIN, ELECTRON, ["assets/**"])).toBe(true);
  });
  test("NOT covered: ./tools/y with no matching entry", () => {
    expect(isCovered("./tools/y", MAIN, ELECTRON, ["main.js", "assets/**"])).toBe(false);
  });
});
