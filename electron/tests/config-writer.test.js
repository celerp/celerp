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
