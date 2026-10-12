// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The first start after an update can rebuild every record before the API listens,
// which takes minutes on a large company. The app keeps waiting while the API is
// alive and shows the rebuild's progress, instead of closing after 30 seconds.
"use strict";

const { waitForApi, rebuildProgress } = require("../api-wait");

const fast = { intervalMs: 1, slowAfterMs: 5 };

test("keeps waiting well past 30 seconds' worth of checks while the API is alive", async () => {
  let checks = 0;
  await expect(waitForApi({ ...fast, isOpen: async () => ++checks > 200, hasExited: () => false }))
    .resolves.toBeUndefined();
  expect(checks).toBeGreaterThan(60);
});

test("stops waiting as soon as the API process has exited", async () => {
  let checks = 0;
  await expect(waitForApi({ ...fast, isOpen: async () => false, hasExited: () => ++checks > 3 }))
    .rejects.toThrow("stopped before it was ready");
  expect(checks).toBeLessThan(10);
});

test("gives up at the upper limit when the API never opens", async () => {
  await expect(waitForApi({ ...fast, maxMs: 30, isOpen: async () => false, hasExited: () => false }))
    .rejects.toThrow("did not start");
});

test("tells the user it is still starting once the wait is long", async () => {
  const onSlow = jest.fn();
  let checks = 0;
  await waitForApi({ ...fast, onSlow, isOpen: async () => ++checks > 50, hasExited: () => false });
  expect(onSlow).toHaveBeenCalledTimes(1);
});

test("reads the rebuild progress the API logs, last line wins", () => {
  const chunk = "INFO:celerp.projections.engine:Rebuilding records: 0 of 250000 ledger entries\n" +
    "INFO:celerp.projections.engine:Rebuilding records: 40000 of 250000 ledger entries\n";
  expect(rebuildProgress(chunk)).toEqual({ done: 40000, total: 250000 });
  expect(rebuildProgress("INFO:celerp.modules.loader:Module 'x': API routes registered\n")).toBeNull();
});
