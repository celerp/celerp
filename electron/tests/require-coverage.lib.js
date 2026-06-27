// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Pure matcher for the L0 gate: "every relative require in a packaged electron JS
// file must be listed in build.files." Factored out so it can be unit-tested with
// fixtures (no filesystem, no build). See packaged-requires.test.js for the real
// run and require-coverage.test.js for the unit tests.
"use strict";

const path = require("path");

/** Return the relative require/import specifiers ("./x", "../y/z") found in src. */
function relativeRequires(src) {
  const re = /require\(\s*['"](\.\.?\/[^'"]+)['"]\s*\)/g;
  const out = [];
  let m;
  while ((m = re.exec(src))) out.push(m[1]);
  return out;
}

/**
 * Is `reqPath` (required from `fromFile`) covered by electron-builder `files`?
 * `files` entries are electron-dir-relative posix paths or "/**" globs.
 */
function isCovered(reqPath, fromFile, electronDir, files) {
  const abs = path.resolve(path.dirname(fromFile), reqPath);
  const rel = path.relative(electronDir, abs).split(path.sep).join("/");
  const candidates = rel.endsWith(".js") ? [rel] : [rel + ".js", rel + "/index.js"];
  return candidates.some((c) =>
    files.some((f) => f === c || (f.endsWith("/**") && c.startsWith(f.slice(0, -3))))
  );
}

module.exports = { relativeRequires, isCovered };
