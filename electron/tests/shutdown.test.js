// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const { spawn } = require("child_process");
const { createShutdown, stopChild } = require("../shutdown");

// A real server stand-in: prints "ready" once its signal handling is in place.
function server({ ignoreTerm = false } = {}) {
  const code = ignoreTerm
    ? "process.on('SIGTERM', () => {}); console.log('ready'); setInterval(() => {}, 1000);"
    : "console.log('ready'); setInterval(() => {}, 1000);";
  const child = spawn(process.execPath, ["-e", code], { stdio: ["ignore", "pipe", "ignore"] });
  return new Promise((resolve) => child.stdout.once("data", () => resolve(child)));
}

function alive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}

const children = [];
afterEach(() => {
  for (const child of children.splice(0)) if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
});

async function servers(...options) {
  const started = await Promise.all(options.map(server));
  children.push(...started);
  return started;
}

describe("stopChild", () => {
  test("a server that stops on SIGTERM is gone when it resolves", async () => {
    const [child] = await servers({});
    expect(await stopChild(child, 2000)).toBe(true);
    expect(child.signalCode).toBe("SIGTERM");
    expect(alive(child.pid)).toBe(false);
  });

  test("a server that ignores SIGTERM is killed after the grace period", async () => {
    const [child] = await servers({ ignoreTerm: true });
    expect(await stopChild(child, 200)).toBe(true);
    expect(child.signalCode).toBe("SIGKILL");
    expect(alive(child.pid)).toBe(false);
  });

  test("a server that already exited or never started resolves at once", async () => {
    const [child] = await servers({});
    await stopChild(child, 2000);
    expect(await stopChild(child, 2000)).toBe(true);
    expect(await stopChild(null, 2000)).toBe(true);
  });
});

describe("createShutdown", () => {
  test("stops the servers, then the database, once however often it is asked", async () => {
    const [api, ui] = await servers({}, {});
    const order = [];
    const stopDatabase = jest.fn(async () => {
      order.push(`database (servers alive: ${alive(api.pid) || alive(ui.pid)})`);
    });
    const lifecycle = createShutdown({ children: () => [api, ui], stopDatabase, log: () => {} });

    expect(lifecycle.stopping).toBe(false);
    const first = lifecycle.shutdown();
    const second = lifecycle.shutdown();
    expect(second).toBe(first);
    expect(lifecycle.stopping).toBe(true);
    expect(lifecycle.stopped).toBe(false);
    await first;
    await lifecycle.shutdown();

    expect(stopDatabase).toHaveBeenCalledTimes(1);
    expect(order).toEqual(["database (servers alive: false)"]);
    expect(lifecycle.stopped).toBe(true);
  });

  test("a database stop that fails is logged and the shutdown still ends", async () => {
    const [api] = await servers({});
    const log = jest.fn();
    const lifecycle = createShutdown({
      children: () => [api],
      stopDatabase: async () => { throw new Error("pg stop failed"); },
      log,
    });
    await lifecycle.shutdown();
    expect(lifecycle.stopped).toBe(true);
    expect(alive(api.pid)).toBe(false);
    expect(log).toHaveBeenCalledWith("[shutdown] database stop failed: pg stop failed");
  });

  test("a database stop that never ends is given up on after its bound", async () => {
    const log = jest.fn();
    const lifecycle = createShutdown({
      children: () => [],
      stopDatabase: () => new Promise(() => {}),
      databaseMs: 100,
      log,
    });
    await lifecycle.shutdown();
    expect(lifecycle.stopped).toBe(true);
    expect(log).toHaveBeenCalledWith("[shutdown] the database did not stop in time");
  });

  test("a server that cannot be stopped is logged and the database is still stopped", async () => {
    const stuck = { exitCode: null, signalCode: null, once: () => {}, kill: jest.fn() };
    const stopDatabase = jest.fn(async () => {});
    const log = jest.fn();
    const lifecycle = createShutdown({ children: () => [stuck], stopDatabase, graceMs: 50, log });
    await lifecycle.shutdown();
    expect(stuck.kill.mock.calls).toEqual([[], ["SIGKILL"]]);
    expect(log).toHaveBeenCalledWith("[shutdown] a server did not exit");
    expect(stopDatabase).toHaveBeenCalledTimes(1);
  });

  test("a restart under way finishes first, and the server it started is stopped too", async () => {
    let api = null;
    let restarted;
    const restart = new Promise((resolve) => { restarted = resolve; });
    const stopDatabase = jest.fn(async () => {});
    const lifecycle = createShutdown({ children: () => [api], stopDatabase, log: () => {} });
    lifecycle.trackRestart(restart);

    const done = lifecycle.shutdown();
    [api] = await servers({});
    restarted();
    await done;

    expect(alive(api.pid)).toBe(false);
    expect(stopDatabase).toHaveBeenCalledTimes(1);
  });
});
