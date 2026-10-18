// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The first start after an update can rebuild every record before the API listens,
// which takes minutes on a large company. The app keeps waiting while the API is
// alive and making progress, and shows that progress, instead of closing after
// 30 seconds.
"use strict";

const { waitForApi, rebuildProgress, lineSplitter, outputTail } = require("../api-wait");

const MIN = 60 * 1000;

beforeEach(() => jest.useFakeTimers());
afterEach(() => jest.useRealTimers());

/** Start a wait and record how it ended, so the clock can run before it settles. */
function track(opts) {
  const result = { state: "waiting" };
  waitForApi(opts).then(
    () => { result.state = "open"; },
    (e) => { result.state = "rejected"; result.error = e.message; },
  );
  return result;
}

test("a live API that opens after 45 seconds is still waited for", async () => {
  const opensAt = Date.now() + 45 * 1000;
  const r = track({ isOpen: async () => Date.now() >= opensAt, hasExited: () => false });
  await jest.advanceTimersByTimeAsync(31 * 1000);
  expect(r.state).toBe("waiting");
  await jest.advanceTimersByTimeAsync(15 * 1000);
  expect(r.state).toBe("open");
});

test("a rebuild that keeps reporting progress is never cut off, however long it runs", async () => {
  let progressAt = 0;
  const r = track({ isOpen: async () => false, hasExited: () => false, lastProgressAt: () => progressAt });
  for (let minute = 0; minute < 120; minute++) {
    progressAt = Date.now();
    await jest.advanceTimersByTimeAsync(MIN);
  }
  expect(r.state).toBe("waiting");
});

test("an API that goes quiet gives up once the quiet limit passes since its last progress", async () => {
  let progressAt = 0;
  const r = track({ isOpen: async () => false, hasExited: () => false, lastProgressAt: () => progressAt });
  await jest.advanceTimersByTimeAsync(20 * MIN);
  progressAt = Date.now();
  await jest.advanceTimersByTimeAsync(29 * MIN);
  expect(r.state).toBe("waiting");
  await jest.advanceTimersByTimeAsync(2 * MIN);
  expect(r.state).toBe("rejected");
  expect(r.error).toMatch("made no progress for 30 minutes");
});

test("stops waiting as soon as the API process has exited", async () => {
  let exited = false;
  const r = track({ isOpen: async () => false, hasExited: () => exited });
  await jest.advanceTimersByTimeAsync(5 * 1000);
  exited = true;
  await jest.advanceTimersByTimeAsync(1000);
  expect(r.state).toBe("rejected");
  expect(r.error).toMatch("stopped before it was ready");
});

test("tells the user it is still starting once, after 30 seconds", async () => {
  const onSlow = jest.fn();
  track({ isOpen: async () => false, hasExited: () => false, onSlow });
  await jest.advanceTimersByTimeAsync(29 * 1000);
  expect(onSlow).not.toHaveBeenCalled();
  await jest.advanceTimersByTimeAsync(5 * MIN);
  expect(onSlow).toHaveBeenCalledTimes(1);
});

test("reads the rebuild progress the API logs", () => {
  expect(rebuildProgress("INFO:celerp.projections.engine:Rebuilding records: 40000 of 250000 ledger entries"))
    .toEqual({ done: 40000, total: 250000 });
  expect(rebuildProgress("INFO:celerp.modules.loader:Module 'x': API routes registered")).toBeNull();
});

// A fresh install replays an empty ledger and logs "0 of 0"; there is nothing to
// update, so nothing is shown.
test("an empty rebuild has no progress to show", () => {
  expect(rebuildProgress("INFO:celerp.projections.engine:Rebuilding records: 0 of 0 ledger entries")).toBeNull();
  expect(rebuildProgress("INFO:celerp.projections.engine:Rebuilding records: 0 of 12 ledger entries"))
    .toEqual({ done: 0, total: 12 });
});

test("a progress line split across two output chunks is still read whole", () => {
  const seen = [];
  const feed = lineSplitter((line) => { const p = rebuildProgress(line); if (p) seen.push(p); });
  feed("INFO:celerp.projections.engine:Rebuilding records: 4");
  feed("0000 of 250000 ledger entries\r\nINFO:other\nINFO:celerp.projections.engine:Rebuilding ");
  feed("records: 41000 of 250000 ledger entries\n");
  expect(seen).toEqual([{ done: 40000, total: 250000 }, { done: 41000, total: 250000 }]);
});

test("a stream that never ends a line keeps only a bounded tail", () => {
  const lines = [];
  const feed = lineSplitter((line) => lines.push(line));
  for (let i = 0; i < 100; i++) feed("x".repeat(10000));
  feed("\n");
  expect(lines).toHaveLength(1);
  expect(lines[0].length).toBeLessThanOrEqual(64 * 1024);
});

test("only the end of a server's output is kept for its startup error", () => {
  const output = outputTail(10);
  output.add("0123456789");
  output.add("abcde");
  expect(output.text()).toBe("56789abcde");
  const big = outputTail();
  for (let i = 0; i < 1000; i++) big.add("y".repeat(1000));
  expect(big.text()).toHaveLength(3000);
});
