// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BSL-1.1
"use strict";

const path = require("path");
const { EventEmitter } = require("events");
const { restartSentinelPath, watchForRestart } = require("../restart");

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
