// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const path = require("path");
const { EventEmitter } = require("events");
const { restartSentinelPath, watchForRestart } = require("../restart");
const { reopenData } = require("../boot");

// ── restartSentinelPath ───────────────────────────────────────────────────────

describe("restartSentinelPath", () => {
  test("Mac/Linux: uses XDG_CONFIG_HOME when set", () => {
    const proc = { platform: "linux", env: { XDG_CONFIG_HOME: "/custom/config", HOME: "/home/user" } };
    expect(restartSentinelPath(proc)).toBe(path.join("/custom/config", "celerp", ".restart_requested"));
  });

  test("Mac/Linux: falls back to ~/.config when XDG_CONFIG_HOME unset", () => {
    const proc = { platform: "darwin", env: { HOME: "/Users/nikoli" } };
    expect(restartSentinelPath(proc)).toBe(path.join("/Users/nikoli", ".config", "celerp", ".restart_requested"));
  });

  test("Windows: uses APPDATA", () => {
    const proc = { platform: "win32", env: { APPDATA: "C:\\Users\\Nikoli\\AppData\\Roaming" } };
    expect(restartSentinelPath(proc)).toBe(
      path.join("C:\\Users\\Nikoli\\AppData\\Roaming", "celerp", ".restart_requested")
    );
  });

  test("Windows: falls back to USERPROFILE when APPDATA unset", () => {
    const proc = { platform: "win32", env: { USERPROFILE: "C:\\Users\\Nikoli" } };
    expect(restartSentinelPath(proc)).toBe(
      path.join("C:\\Users\\Nikoli", "AppData", "Roaming", "celerp", ".restart_requested")
    );
  });
});

// ── watchForRestart ───────────────────────────────────────────────────────────

function makeFakeProcess() {
  const emitter = new EventEmitter();
  emitter.kill = jest.fn();
  return emitter;
}

function makeTestDeps({ sentinelExists = false, sentinelPath = "/tmp/test-sentinel" } = {}) {
  let currentApiProcess = makeFakeProcess();
  let currentUiProcess = makeFakeProcess();

  const fs = {
    existsSync: jest.fn(() => sentinelExists),
    unlinkSync: jest.fn(),
  };

  const startApi = jest.fn(async () => { currentApiProcess = makeFakeProcess(); });
  const startUi = jest.fn(async () => {});
  const onCrash = jest.fn();

  const deps = {
    getApiProcess: () => currentApiProcess,
    getUiProcess: () => currentUiProcess,
    setUiProcess: jest.fn((p) => { currentUiProcess = p; }),
    startApi,
    startUi,
    sentinelPath,
    onCrash,
    isStopping: () => false,
    trackRestart: (restart) => restart,
    fs,
  };

  return { deps, fs, startApi, startUi, onCrash, getApiProcess: () => currentApiProcess, getUiProcess: () => currentUiProcess };
}

describe("watchForRestart", () => {
  test("does nothing when getApiProcess returns null", () => {
    const { deps } = makeTestDeps();
    deps.getApiProcess = () => null;
    expect(() => watchForRestart("postgres://x", deps)).not.toThrow();
  });

  test("sentinel present: kills UI, respawns API and UI, re-attaches watcher", async () => {
    const { deps, fs, startApi, startUi, onCrash, getApiProcess } = makeTestDeps({ sentinelExists: true });
    const originalApi = getApiProcess();

    watchForRestart("postgres://x", deps);
    originalApi.emit("exit", 0);

    // Allow async handler to run
    await new Promise(r => setTimeout(r, 10));

    expect(fs.unlinkSync).toHaveBeenCalledWith(deps.sentinelPath);
    expect(startApi).toHaveBeenCalledWith("postgres://x");
    expect(startUi).toHaveBeenCalledWith("postgres://x");
    expect(onCrash).not.toHaveBeenCalled();
  });

  test("the API is announced once its exit is watched, and again after each restart", async () => {
    const { deps, getApiProcess } = makeTestDeps({ sentinelExists: true });
    const watched = [];
    deps.onWatching = jest.fn(() => watched.push(getApiProcess().listenerCount("exit")));
    const first = getApiProcess();

    watchForRestart("postgres://x", deps);
    expect(watched).toEqual([1]);

    first.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(getApiProcess()).not.toBe(first);
    expect(watched).toEqual([1, 1]);
  });

  test("the stopped API's port is taken away before the restart starts the next one", async () => {
    const { deps, getApiProcess } = makeTestDeps({ sentinelExists: true });
    const order = [];
    deps.onStopped = jest.fn(() => order.push("stopped"));
    deps.onWatching = jest.fn(() => order.push("watching"));
    const startApi = deps.startApi;
    deps.startApi = jest.fn(async (url) => { order.push("startApi"); await startApi(url); });
    const first = getApiProcess();

    watchForRestart("postgres://x", deps);
    first.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(order).toEqual(["watching", "stopped", "startApi", "watching"]);
  });

  test.each([
    ["the app is quitting", { stopping: true, code: 0 }],
    ["the API crashed", { stopping: false, code: 1 }],
    ["the API stopped on its own", { stopping: false, code: 0 }],
  ])("the stopped API's port is taken away when %s", async (_why, { stopping, code }) => {
    const { deps, getApiProcess, startApi } = makeTestDeps({ sentinelExists: false });
    deps.onStopped = jest.fn();
    deps.isStopping = () => stopping;

    watchForRestart("postgres://x", deps);
    getApiProcess().emit("exit", code);
    await new Promise(r => setTimeout(r, 10));

    expect(deps.onStopped).toHaveBeenCalledTimes(1);
    expect(startApi).not.toHaveBeenCalled();
  });

  test("a restart that comes back without an API announces nothing", async () => {
    const { deps } = makeTestDeps({ sentinelExists: true });
    deps.onWatching = jest.fn();
    let api = deps.getApiProcess();
    const first = api;
    deps.getApiProcess = () => api;
    deps.startApi = jest.fn(async () => { api = null; });
    watchForRestart("postgres://x", deps);

    first.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(deps.startApi).toHaveBeenCalledTimes(1);
    expect(deps.onWatching).toHaveBeenCalledTimes(1);
  });

  test("onRestart callback is invoked after successful respawn", async () => {
    const { deps } = makeTestDeps({ sentinelExists: true });
    const onRestart = jest.fn();
    deps.onRestart = onRestart;
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(onRestart).toHaveBeenCalledTimes(1);
  });

  test("onRestart is NOT called when sentinel is absent", async () => {
    const { deps } = makeTestDeps({ sentinelExists: false });
    const onRestart = jest.fn();
    deps.onRestart = onRestart;
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(onRestart).not.toHaveBeenCalled();
  });

  test("onRestart is NOT called when respawn fails", async () => {
    const { deps } = makeTestDeps({ sentinelExists: true });
    deps.startApi = jest.fn(async () => { throw new Error("boom"); });
    const onRestart = jest.fn();
    deps.onRestart = onRestart;
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(onRestart).not.toHaveBeenCalled();
  });

  test("a refused restart starts no UI and stops watching", async () => {
    const { deps, startUi, onCrash } = makeTestDeps({ sentinelExists: true });
    const onRestart = jest.fn();
    deps.onRestart = onRestart;
    // The restart's own check refused the database: nothing came back up.
    deps.startApi = jest.fn(async () => reopenData({
      mayOpenData: async () => false,
      runMigrations: () => { throw new Error("must not migrate"); },
      startApi: async () => { throw new Error("must not start the API"); },
    }));
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(deps.startApi).toHaveBeenCalledTimes(1);
    expect(startUi).not.toHaveBeenCalled();
    expect(onRestart).not.toHaveBeenCalled();
    expect(onCrash).not.toHaveBeenCalled();
  });

  test("sentinel absent, exit code 0: no crash, no respawn", async () => {
    const { deps, startApi, startUi, onCrash } = makeTestDeps({ sentinelExists: false });
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(startApi).not.toHaveBeenCalled();
    expect(startUi).not.toHaveBeenCalled();
    expect(onCrash).not.toHaveBeenCalled();
  });

  test("sentinel absent, non-zero exit: calls onCrash", async () => {
    const { deps, onCrash } = makeTestDeps({ sentinelExists: false });
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 1);
    await new Promise(r => setTimeout(r, 10));

    expect(onCrash).toHaveBeenCalledWith(expect.any(Error));
    expect(onCrash.mock.calls[0][0].message).toMatch(/exited unexpectedly with code 1/);
  });

  test("startApi failure during respawn: calls onCrash", async () => {
    const { deps, onCrash } = makeTestDeps({ sentinelExists: true });
    deps.startApi = jest.fn(async () => { throw new Error("port taken"); });
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await new Promise(r => setTimeout(r, 10));

    expect(onCrash).toHaveBeenCalledWith(expect.objectContaining({ message: "port taken" }));
  });

  test("watcher re-attaches after restart (second restart also works)", async () => {
    let apiEmitter = makeFakeProcess();
    const uiEmitter = makeFakeProcess();
    const fs = { existsSync: jest.fn(() => true), unlinkSync: jest.fn() };
    const startApi = jest.fn(async () => { apiEmitter = makeFakeProcess(); });
    const startUi = jest.fn(async () => {});
    const onCrash = jest.fn();

    const deps = {
      getApiProcess: () => apiEmitter,
      getUiProcess: () => uiEmitter,
      setUiProcess: jest.fn(),
      startApi,
      startUi,
      sentinelPath: "/tmp/sentinel",
      onCrash,
      isStopping: () => false,
      trackRestart: (restart) => restart,
      fs,
    };

    // First restart
    watchForRestart("postgres://x", deps);
    apiEmitter.emit("exit", 0);
    await new Promise(r => setTimeout(r, 20));
    expect(startApi).toHaveBeenCalledTimes(1);

    // Second restart — watcher should have re-attached to new apiEmitter
    apiEmitter.emit("exit", 0);
    await new Promise(r => setTimeout(r, 20));
    expect(startApi).toHaveBeenCalledTimes(2);
    expect(onCrash).not.toHaveBeenCalled();
  });
});

describe("watchForRestart once the app is stopping", () => {
  test("an exit after shutdown started is neither a restart nor a crash", async () => {
    const { deps, fs, startApi, onCrash } = makeTestDeps({ sentinelExists: true });
    deps.isStopping = () => true;
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 1);
    await new Promise(r => setTimeout(r, 10));

    expect(fs.unlinkSync).not.toHaveBeenCalled();
    expect(startApi).not.toHaveBeenCalled();
    expect(onCrash).not.toHaveBeenCalled();
  });

  test("a shutdown during a restart stops it before the UI starts", async () => {
    const { deps, startUi, onCrash } = makeTestDeps({ sentinelExists: true });
    let stopping = false;
    deps.isStopping = () => stopping;
    const startApi = deps.startApi;
    deps.startApi = jest.fn(async (url) => { await startApi(url); stopping = true; });
    const tracked = [];
    deps.trackRestart = (restart) => tracked.push(restart);
    const onRestart = jest.fn();
    deps.onRestart = onRestart;
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    expect(tracked).toHaveLength(1);
    await tracked[0];

    expect(deps.startApi).toHaveBeenCalledTimes(1);
    expect(startUi).not.toHaveBeenCalled();
    expect(onRestart).not.toHaveBeenCalled();
    expect(onCrash).not.toHaveBeenCalled();
  });

  test("a restart that fails because the app is stopping is not reported as a crash", async () => {
    const { deps, onCrash } = makeTestDeps({ sentinelExists: true });
    let stopping = false;
    deps.isStopping = () => stopping;
    deps.startApi = jest.fn(async () => { stopping = true; throw new Error("killed"); });
    const tracked = [];
    deps.trackRestart = (restart) => tracked.push(restart);
    const api = deps.getApiProcess();

    watchForRestart("postgres://x", deps);
    api.emit("exit", 0);
    await tracked[0];

    expect(onCrash).not.toHaveBeenCalled();
  });
});

// ── classifyNavigation ────────────────────────────────────────────────────────

describe("classifyNavigation", () => {
  const { classifyNavigation } = require("../restart");

  test("current UI port is allowed in-window", () => {
    expect(classifyNavigation("http://127.0.0.1:36303/login?imported=1", 36303)).toBe("allow");
  });

  test("a FORMER UI port is denied, never opened externally", () => {
    // The restore-restart bug: a redirect issued before the restart still points at
    // the dead old port; it must be dropped, not handed to the OS browser.
    const former = new Set([36303]);
    expect(classifyNavigation("http://127.0.0.1:36303/login?imported=1", 41211, former)).toBe("deny");
  });

  test("loopback ports the app never served stay external (user links to local tools)", () => {
    const former = new Set([36303]);
    expect(classifyNavigation("http://127.0.0.1:3000/dashboard", 41211, former)).toBe("external");
    expect(classifyNavigation("http://127.0.0.1:631/", 41211, former)).toBe("external");
  });

  test("port reuse: current port wins even if it was formerly used", () => {
    const former = new Set([36303]);
    expect(classifyNavigation("http://127.0.0.1:36303/", 36303, former)).toBe("allow");
  });

  test("ports compare exactly, not by string prefix", () => {
    // startsWith("http://127.0.0.1:" + 3630) would wrongly match :36303
    expect(classifyNavigation("http://127.0.0.1:36303/x", 3630)).toBe("external");
    expect(classifyNavigation("http://127.0.0.1:3630/x", 3630)).toBe("allow");
  });

  test("non-loopback http(s) is external", () => {
    expect(classifyNavigation("https://celerp.com/docs", 36303)).toBe("external");
    expect(classifyNavigation("http://192.168.1.10:8080/", 36303)).toBe("external");
    expect(classifyNavigation("http://localhost:9000/", 36303)).toBe("external");
  });

  test("non-http(s) schemes and garbage are denied", () => {
    expect(classifyNavigation("javascript:alert(1)", 36303)).toBe("deny");
    expect(classifyNavigation("file:///etc/passwd", 36303)).toBe("deny");
    expect(classifyNavigation("mailto:a@b.c", 36303)).toBe("deny");
    expect(classifyNavigation("not a url", 36303)).toBe("deny");
  });

  test("pre-boot phase (uiPort null): loopback stays external, like before", () => {
    expect(classifyNavigation("http://127.0.0.1:8080/", null)).toBe("external");
  });

  test("loopback with no explicit port is external (port 80 is never ours)", () => {
    expect(classifyNavigation("http://127.0.0.1/", 36303)).toBe("external");
  });
});

describe("restartUrl", () => {
  const { restartUrl } = require("../restart");

  test("keeps the page the window was on, on the new UI port", () => {
    expect(restartUrl("http://127.0.0.1:41001/migrations/run-1?step=2#log", 41005, new Set([41001])))
      .toBe("http://127.0.0.1:41005/migrations/run-1?step=2#log");
  });

  test("a page already on the new port keeps its path", () => {
    expect(restartUrl("http://127.0.0.1:41005/docs?type=invoice", 41005, new Set()))
      .toBe("http://127.0.0.1:41005/docs?type=invoice");
  });

  test("anything that is not the app's own page goes to the root", () => {
    for (const url of ["", "about:blank", "file:///app/loading.html", "http://127.0.0.1:9999/x",
                       "https://celerp.com/pricing", "not a url"]) {
      expect(restartUrl(url, 41005, new Set([41001]))).toBe("http://127.0.0.1:41005");
    }
  });
});
