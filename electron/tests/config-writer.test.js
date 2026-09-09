// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn } = require("child_process");

// Requiring the module at load time keeps the whole suite red at merge-base,
// where electron/config-writer.js does not yet exist.
const { writeConfig } = require("../config-writer");

const CLI = path.join(__dirname, "..", "config-writer.js");

function sandbox() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cfgwriter-"));
  const cfg = path.join(dir, "celerp-config.json");
  fs.writeFileSync(cfg, JSON.stringify({ db_mode: "local" }));
  return { dir, cfg, lock: cfg + ".lock" };
}

describe("config-writer locked writer", () => {
  test("held-lock-defers-write: the CLI waits on a fresh lock, then writes once released", (done) => {
    const { cfg, lock } = sandbox();
    fs.writeFileSync(lock, "held"); // fresh mtime -> not stale
    const child = spawn(process.execPath, [CLI, cfg, "node_key", "1"]);
    let released = false;

    setTimeout(() => {
      // While the lock is held the writer must not have applied its key.
      const mid = JSON.parse(fs.readFileSync(cfg, "utf8"));
      expect(mid.node_key).toBeUndefined();
      released = true;
      fs.unlinkSync(lock);
    }, 300);

    child.on("exit", (code) => {
      expect(released).toBe(true); // it waited past the held window
      expect(code).toBe(0);
      const merged = JSON.parse(fs.readFileSync(cfg, "utf8"));
      expect(merged.node_key).toBe(1);
      expect(merged.db_mode).toBe("local"); // prior key preserved
      done();
    });
  });

  test("throws on budget timeout while the lock stays held, leaving config intact", () => {
    const { cfg, lock } = sandbox();
    fs.writeFileSync(lock, "held"); // fresh, stays well under the stale window
    expect(() => writeConfig(cfg, { a: 1 }, { budgetMs: 600, retryMs: 50 })).toThrow();
    expect(JSON.parse(fs.readFileSync(cfg, "utf8"))).toEqual({ db_mode: "local" });
  });

  test("a stale lock (mtime aged past the threshold) is taken over and released", () => {
    const { cfg, lock } = sandbox();
    fs.writeFileSync(lock, "stale");
    const old = new Date(Date.now() - 30000);
    fs.utimesSync(lock, old, old);
    writeConfig(cfg, { taken: true });
    const merged = JSON.parse(fs.readFileSync(cfg, "utf8"));
    expect(merged.taken).toBe(true);
    expect(merged.db_mode).toBe("local");
    expect(fs.existsSync(lock)).toBe(false); // released in the finally path
  });
});

// check_js_config_lock_single_writer: a superseded owner's release must not
// delete the successor's lock. Uses the internal acquire/release primitives so
// the token-verified unlink is exercised at the same granularity as the Python
// side (tests/test_config_store.py::test_config_lock_stale_takeover_single_writer).
const { acquireLock, releaseLock } = require("../config-writer");

describe("config-writer single-writer owner token", () => {
  test("check_js_config_lock_single_writer: superseded release leaves the successor's lock", () => {
    const { lock } = sandbox();

    // Owner A acquires and writes its token.
    const { fd: fdA, token: tokenA } = acquireLock(lock, {});
    expect(fs.readFileSync(lock, "utf8")).toBe(tokenA);

    // Age the lock so a takeover is permitted; owner B takes it over via the
    // exclusive-create path, writing B's own distinct token.
    const old = new Date(Date.now() - 30000);
    fs.utimesSync(lock, old, old);
    const { fd: fdB, token: tokenB } = acquireLock(lock, {});
    expect(tokenB).not.toBe(tokenA); // takeover wrote a distinct owner token
    expect(fs.readFileSync(lock, "utf8")).toBe(tokenB);

    // Superseded owner A releases: its unlink is token-verified and must be a
    // no-op, so B's live lock survives.
    releaseLock(fdA, lock, tokenA);
    expect(fs.existsSync(lock)).toBe(true); // successor's lock not deleted
    expect(fs.readFileSync(lock, "utf8")).toBe(tokenB);

    // B's own release removes its lock cleanly.
    releaseLock(fdB, lock, tokenB);
    expect(fs.existsSync(lock)).toBe(false);
  });

  test("release only unlinks while the on-disk token is still ours", () => {
    const { lock } = sandbox();
    const { fd, token } = acquireLock(lock, {});
    fs.writeFileSync(lock, "different-owner-token");
    releaseLock(fd, lock, token);
    expect(fs.existsSync(lock)).toBe(true);
    expect(fs.readFileSync(lock, "utf8")).toBe("different-owner-token");
    fs.unlinkSync(lock);
  });
});

// check_js_config_dir_fsync: writeConfig fsyncs the containing directory after
// the atomic rename so the rename is durable, mirroring the Python side
// (tests/test_config_store.py::test_config_dir_fsynced_after_rename).
describe("config-writer directory fsync", () => {
  test("check_js_config_dir_fsync: the parent directory is fsynced after rename", () => {
    const { dir, cfg } = sandbox();
    const openSpy = jest.spyOn(fs, "openSync");
    const fsyncSpy = jest.spyOn(fs, "fsyncSync");
    let dirFsynced = false;
    try {
      // Track which fds correspond to the directory open, then confirm one of
      // them is fsynced.
      const dirFds = new Set();
      openSpy.mockImplementation((p, ...rest) => {
        const fd = jest.requireActual("fs").openSync(p, ...rest);
        if (p === dir) dirFds.add(fd);
        return fd;
      });
      fsyncSpy.mockImplementation((fd) => {
        if (dirFds.has(fd)) dirFsynced = true;
        return jest.requireActual("fs").fsyncSync(fd);
      });
      writeConfig(cfg, { durable: true });
    } finally {
      openSpy.mockRestore();
      fsyncSpy.mockRestore();
    }
    expect(dirFsynced).toBe(true);
    expect(JSON.parse(fs.readFileSync(cfg, "utf8")).durable).toBe(true);
  });
});
