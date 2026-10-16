// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";
const { parseHandoffUrl, handoffFromArgs, handoffRestorePath, handoffOpenScript } = require("../handoff-protocol");
const key = Buffer.from(Array.from({ length: 32 }, (_, i) => i)).toString("base64url");
const id = "94c404d7-94d1-418e-8863-72ec356d5aa9";
const link = `celerp://handoff/${id}#${key}`;

test("only a canonical company handoff can open", () => {
  expect(parseHandoffUrl(link)).toBe(link);
  expect(handoffFromArgs(["/Applications/Celerp", "--unrelated", link])).toBe(link);
  expect(handoffFromArgs(["/Applications/Celerp", "--unrelated"])).toBeNull();
});

test.each([
  `https://relay.celerp.com/handoffs/${id}/open#${key}`,
  `celerp://handoff/${id}?secret=x#${key}`,
  `celerp://handoff/${id}/extra#${key}`,
  `celerp://handoff/${id}#${key}x`,
  `celerp://handoff/${id}#${key.slice(0, -1)}@`,
  `celerp://other/${id}#${key}`,
  `celerp://handoff/${id.toUpperCase()}#${key}`,
  `celerp://handoff/${id}#`,
  `celerp://handoff/${id}#${key}?x=1`,
  `celerp://handoff/${id}#${key.slice(0, -1)}h`,
  "celerp://handoff/not-a-uuid#" + key,
])("rejects malformed desktop handoff %s", (bad) => {
  expect(parseHandoffUrl(bad)).toBeNull();
});

test.each([
  ["/login", null],
  ["/setup", "/setup/restore-backup"],
  ["/setup/restore-backup", "/setup/restore-backup"],
  ["/setup/new-company", "/setup/new-company/restore-backup"],
  ["/setup/start-company", "/setup/start-company/restore-backup"],
  ["/setup/start-company/welcome", "/setup/start-company/restore-backup"],
  ["/setup/new-company/start", "/setup/new-company/restore-backup"],
  ["/dashboard", "/settings/restore-backup"],
  ["/settings/restore-backup", "/settings/restore-backup"],
])("selects an existing restore entry for %s", (path, expected) => {
  expect(handoffRestorePath(path)).toBe(expected);
});

function runOpenScript(page) {
  const origin = "http://127.0.0.1:8123";
  const script = handoffOpenScript(origin, link);
  const submitted = [];
  const field = { value: "", focused: false, focus() { this.focused = true; }, dispatchEvent() {} };
  const form = {
    checkValidity: () => page.valid,
    requestSubmit: () => submitted.push(field.value),
    reportValidity: () => { page.reported = true; },
  };
  field.form = form;
  const document = { querySelector: (sel) => (sel === "[name=handoff_link]" ? field : null) };
  const location = { origin: page.origin || origin };
  // The script runs in the page; evaluate it against a minimal page.
  const shown = new Function("document", "location", "Event", `return ${script};`)(
    document, location, function Event() {});
  return { shown, submitted, field };
}

test("a deep link opens the company copy without a manual Continue", () => {
  const { shown, submitted, field } = runOpenScript({ valid: true });
  expect(shown).toBe(true);
  expect(field.value).toBe(link);
  expect(submitted).toEqual([link]);
});

test("a page still asking for sign-in details keeps the link and asks for them", () => {
  const page = { valid: false };
  const { shown, submitted, field } = runOpenScript(page);
  expect(shown).toBe(true);
  expect(field.value).toBe(link);
  expect(submitted).toEqual([]);
  expect(page.reported).toBe(true);
});

test("a page on another origin is never filled", () => {
  const { shown, submitted, field } = runOpenScript({ valid: true, origin: "https://evil.example" });
  expect(shown).toBe(false);
  expect(field.value).toBe("");
  expect(submitted).toEqual([]);
});
