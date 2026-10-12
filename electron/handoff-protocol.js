// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const FRAGMENT = /^[A-Za-z0-9_-]{43}$/;

function parseHandoffUrl(value) {
  if (typeof value !== "string" || value.length > 512 || !value.startsWith("celerp://handoff/")) return null;
  const match = /^celerp:\/\/handoff\/([^/?#]+)#([^?#]+)$/.exec(value);
  if (!match || !UUID.test(match[1]) || !FRAGMENT.test(match[2])) return null;
  const bytes = Buffer.from(match[2], "base64url");
  if (bytes.length !== 32 || bytes.toString("base64url") !== match[2]) return null;
  return value;
}

function handoffRestorePath(pathname) {
  if (pathname === "/login" || pathname.startsWith("/login/")) return null;
  if (pathname === "/setup/new-company" || pathname.startsWith("/setup/new-company/")) return "/setup/new-company/restore-backup";
  if (pathname === "/setup/start-company" || pathname.startsWith("/setup/start-company/")) return "/setup/start-company/restore-backup";
  if (pathname === "/setup" || pathname.startsWith("/setup/")) return "/setup/restore-backup";
  return "/settings/restore-backup";
}

function handoffFromArgs(args) {
  if (!Array.isArray(args)) return null;
  for (const arg of args) {
    const valid = parseHandoffUrl(arg);
    if (valid) return valid;
  }
  return null;
}

module.exports = { parseHandoffUrl, handoffFromArgs, handoffRestorePath };
