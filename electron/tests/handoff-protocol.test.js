// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";
const { parseHandoffUrl, handoffFromArgs, handoffRestorePath } = require("../handoff-protocol");
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
