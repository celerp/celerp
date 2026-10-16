// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Turns the ways the bundled database fails to start on Windows into a message the
// user can act on. Anything not recognised here keeps its original error.
"use strict";

const fs = require("fs");

// STATUS_DLL_NOT_FOUND (0xC0000135): Windows could not find a DLL the program
// needs. Node reports it unsigned; a signed 32-bit reader sees it negative.
const DLL_NOT_FOUND_CODES = new Set([0xC0000135, 0xC0000135 - 0x100000000]);

const MISSING_COMPONENT =
  "A Windows component Celerp needs is missing. Reinstall Celerp, or install the " +
  "Microsoft Visual C++ Redistributable (x64): https://aka.ms/vs/17/release/vc_redist.x64.exe";

const ADMIN_WITHOUT_UAC =
  "Celerp cannot start from an administrator account with User Account Control " +
  "turned off. Turn on UAC, or sign in with a standard Windows account.";

// Postgres's own refusal to run under an administrator token, initdb failing to
// drop those rights itself, and the failure prefix of
// celerp/win_restricted_launch.py (keep the two sides in step).
const ADMIN_REFUSAL = /administrative permissions|could not re-execute with restricted token|win_restricted_launch:/;

/** The user-facing message for a database start failure, or null when it is not one we recognise. */
function windowsStartFailure({ exitCode, output = "" }) {
  if (DLL_NOT_FOUND_CODES.has(exitCode)) return MISSING_COMPONENT;
  if (ADMIN_REFUSAL.test(output)) return ADMIN_WITHOUT_UAC;
  return null;
}

/** What was written to the log at `logPath` after byte `offset`; empty when it cannot be read. */
function logSince(logPath, offset) {
  try {
    return fs.readFileSync(logPath).subarray(offset).toString("utf8");
  } catch {
    return "";
  }
}

module.exports = { windowsStartFailure, logSince };
