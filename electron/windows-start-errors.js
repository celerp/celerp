// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Turns the ways the bundled database fails to start on Windows into a message the
// user can act on. Anything not recognised here keeps its original error.
"use strict";

const fs = require("fs");
const path = require("path");

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

/**
 * The message for a database that would not start for a reason not recognised
 * above. The folder is named and left exactly as it was; `reason` is the original
 * error. Postgres writes postmaster.opts every time it starts, so a folder without
 * it never finished setting up and can be moved aside for a fresh one; a folder
 * with it holds data that has been used, and moving it would hide that data.
 */
function clusterStartFailure(dataDir, reason) {
  const hasStarted = fs.existsSync(path.join(dataDir, "postmaster.opts"));
  return `Celerp's database in ${dataDir} could not start. Nothing in it was changed or removed. ` +
    (hasStarted
      ? "Restart your PC and start Celerp again. If it still does not start, keep that folder as it is and report the problem at https://github.com/celerp/celerp/issues with the details below."
      : "It never finished setting up, so move that folder somewhere else (keep it) and start Celerp again to set up a new database.") +
    (reason ? ` Details: ${reason}` : "");
}

// Enough of an attempt's log to hold initdb's error, however long the log has grown.
const LOG_SINCE_MAX_BYTES = 64 * 1024;

/**
 * What was written to the log at `logPath` after byte `offset`, at most its last
 * LOG_SINCE_MAX_BYTES; empty when it cannot be read. Earlier attempts are never read.
 */
function logSince(logPath, offset) {
  let fd;
  try {
    fd = fs.openSync(logPath, "r");
    const size = fs.fstatSync(fd).size;
    const start = Math.max(offset, size - LOG_SINCE_MAX_BYTES);
    const buffer = Buffer.alloc(Math.max(size - start, 0));
    const read = fs.readSync(fd, buffer, 0, buffer.length, start);
    return buffer.subarray(0, read).toString("utf8");
  } catch {
    return "";
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

module.exports = { windowsStartFailure, clusterStartFailure, logSince, LOG_SINCE_MAX_BYTES };
