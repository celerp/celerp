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

// The page script that puts a company copy link into the restore form and opens it.
// Opening only downloads the copy and shows what it contains; nothing is restored until
// the owner confirms the preview. A form still asking for sign-in details keeps the link
// and points at the missing field instead.
function handoffOpenScript(origin, link) {
  return "(function () {"
    + "if (location.origin !== " + JSON.stringify(origin) + ") return false;"
    + "const field = document.querySelector('[name=handoff_link]');"
    + "if (!field) return false;"
    + "field.value = " + JSON.stringify(link) + ";"
    + "field.dispatchEvent(new Event('input', { bubbles: true }));"
    + "if (field.form.checkValidity()) { field.form.requestSubmit(); } else { field.form.reportValidity(); }"
    + "return true;"
    + "})()";
}

function handoffFromArgs(args) {
  if (!Array.isArray(args)) return null;
  for (const arg of args) {
    const valid = parseHandoffUrl(arg);
    if (valid) return valid;
  }
  return null;
}

module.exports = { parseHandoffUrl, handoffFromArgs, handoffRestorePath, handoffOpenScript };
