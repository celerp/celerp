// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// L0 gate: every relative require() in a packaged electron JS file must be listed
// in build.files, so it lands in app.asar. This is the static guard for the v1.2.6
// class ("added require('./migrate_cmd'), forgot to add it to build.files").
"use strict";

const fs = require("fs");
const path = require("path");
const { relativeRequires, isCovered } = require("./require-coverage.lib");

const ELECTRON = path.join(__dirname, "..");
const pkg = JSON.parse(fs.readFileSync(path.join(ELECTRON, "package.json"), "utf8"));
const files = pkg.build.files;
// Check every .js file the package ships (the JS entries of build.files).
const jsEntries = files.filter((f) => f.endsWith(".js"));

describe("packaged requires are in build.files", () => {
  expect(jsEntries.length).toBeGreaterThan(0);
  for (const entry of jsEntries) {
    const full = path.join(ELECTRON, entry);
    if (!fs.existsSync(full)) {
      test(`${entry} is listed in build.files but missing on disk`, () => {
        throw new Error(`${entry} listed in build.files but not found at ${full}`);
      });
      continue;
    }
    for (const reqPath of relativeRequires(fs.readFileSync(full, "utf8"))) {
      test(`${entry} -> ${reqPath} is packaged`, () => {
        expect(isCovered(reqPath, full, ELECTRON, files)).toBe(true);
      });
    }
  }
});
