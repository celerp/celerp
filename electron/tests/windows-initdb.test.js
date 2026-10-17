// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The first database setup on Windows either completes or leaves nothing behind:
// initdb writes into a separate folder that is moved into place only when it
// succeeds, so a setup cut short is simply run again on the next start and a
// folder holding someone's data, or one Celerp did not make, is never deleted.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { initialiseClusterOnce, initdbShareDir } = require("../windows-initdb");

function dataDirIn(root = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-pg-"))) {
  const dataDir = path.join(root, "pg");
  fs.mkdirSync(dataDir);
  return dataDir;
}

// Stands in for initdb: fills the folder it is given like a finished cluster.
const goodInitdb = jest.fn(async (target) => {
  fs.mkdirSync(path.join(target, "base"), { recursive: true });
  fs.writeFileSync(path.join(target, "PG_VERSION"), "17\n");
});

beforeEach(() => goodInitdb.mockClear());

test("a first start sets the cluster up and moves it into place", async () => {
  const dataDir = dataDirIn();
  await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb })).resolves.toBe(true);
  expect(goodInitdb).toHaveBeenCalledWith(`${dataDir}.init`);
  expect(fs.readFileSync(path.join(dataDir, "PG_VERSION"), "utf8")).toBe("17\n");
  expect(fs.existsSync(`${dataDir}.init`)).toBe(false);
});

test("the data folder not existing yet is fine", async () => {
  const dataDir = dataDirIn();
  fs.rmdirSync(dataDir);
  await initialiseClusterOnce({ dataDir, runInitdb: goodInitdb });
  expect(fs.existsSync(path.join(dataDir, "PG_VERSION"))).toBe(true);
});

test("an existing cluster is left alone", async () => {
  const dataDir = dataDirIn();
  fs.writeFileSync(path.join(dataDir, "PG_VERSION"), "17\n");
  await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb })).resolves.toBe(false);
  expect(goodInitdb).not.toHaveBeenCalled();
});

test("a failed setup rejects, leaves the data folder as it was and cleans up after itself", async () => {
  const dataDir = dataDirIn();
  const failure = new Error("initdb exited with code 1");
  const badInitdb = async (target) => {
    fs.mkdirSync(target, { recursive: true });
    fs.writeFileSync(path.join(target, "half-written"), "");
    throw failure;
  };
  await expect(initialiseClusterOnce({ dataDir, runInitdb: badInitdb })).rejects.toBe(failure);
  expect(fs.readdirSync(dataDir)).toEqual([]);
  expect(fs.readdirSync(path.dirname(dataDir))).toEqual(["pg"]);
});

// Starts a setup whose initdb writes part of a cluster and then never finishes,
// as when the app is closed or the PC switched off during setup.
async function cutShortSetup(dataDir) {
  let reached;
  const started = new Promise((resolve) => { reached = resolve; });
  initialiseClusterOnce({
    dataDir,
    runInitdb: async (target) => {
      fs.mkdirSync(path.join(target, "base", "1"), { recursive: true });
      fs.writeFileSync(path.join(target, "postgresql.conf"), "partial");
      reached();
      await new Promise(() => {});
    },
  });
  await started;
}

test("a setup cut short (app closed or PC switched off) is run again cleanly on the next start", async () => {
  const dataDir = dataDirIn();
  await cutShortSetup(dataDir);
  // The process that was setting up is gone.
  await initialiseClusterOnce({ dataDir, runInitdb: goodInitdb, isRunning: () => false });
  expect(goodInitdb).toHaveBeenCalledTimes(1);
  expect(fs.existsSync(path.join(dataDir, "PG_VERSION"))).toBe(true);
  expect(fs.existsSync(path.join(dataDir, "postgresql.conf"))).toBe(false);
  expect(fs.readdirSync(path.dirname(dataDir))).toEqual(["pg"]);
});

test("initdb is given an empty setup folder made for it, and the owner and lock files go once it is done", async () => {
  const dataDir = dataDirIn();
  const seen = [];
  await initialiseClusterOnce({
    dataDir,
    runInitdb: async (target) => {
      seen.push(fs.readdirSync(target), fs.existsSync(`${target}.owner`), fs.existsSync(`${target}.lock`));
      await goodInitdb(target);
    },
  });
  expect(seen).toEqual([[], true, true]);
  expect(fs.readdirSync(path.dirname(dataDir))).toEqual(["pg"]);
});

test("a setup folder Celerp did not make is never deleted; the user is told what to do", async () => {
  const dataDir = dataDirIn();
  const staging = `${dataDir}.init`;
  fs.mkdirSync(staging);
  fs.writeFileSync(path.join(staging, "something-of-theirs"), "keep me");
  await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb }))
    .rejects.toThrow(new RegExp(`found ${staging.replace(/[\\.]/g, "\\$&")}, which it did not create\\. Move it`));
  expect(goodInitdb).not.toHaveBeenCalled();
  expect(fs.readFileSync(path.join(staging, "something-of-theirs"), "utf8")).toBe("keep me");
  expect(fs.existsSync(`${staging}.lock`)).toBe(false);
});

test("an owner file naming another folder does not make a setup folder deletable", async () => {
  // The setup folder was removed and something else put in its place later.
  const dataDir = dataDirIn();
  const staging = `${dataDir}.init`;
  await cutShortSetup(dataDir);
  fs.rmSync(staging, { recursive: true });
  fs.rmSync(`${staging}.lock`);
  fs.mkdirSync(staging);
  fs.writeFileSync(path.join(staging, "something-of-theirs"), "keep me");
  await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb })).rejects.toThrow(/did not create/);
  expect(fs.readFileSync(path.join(staging, "something-of-theirs"), "utf8")).toBe("keep me");
});

test("an empty setup folder left before its owner file was written is cleared", async () => {
  const dataDir = dataDirIn();
  fs.mkdirSync(`${dataDir}.init`);
  await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb })).resolves.toBe(true);
  expect(fs.existsSync(path.join(dataDir, "PG_VERSION"))).toBe(true);
});

describe("two starts setting up at once", () => {
  const lockOf = (dataDir) => `${dataDir}.init.lock`;

  test("a second start while the first is setting up stops with a message and touches nothing", async () => {
    const dataDir = dataDirIn();
    fs.writeFileSync(lockOf(dataDir), "4242\n");
    await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb, isRunning: (pid) => pid === 4242 }))
      .rejects.toThrow(`Another Celerp start is setting up the database in ${dataDir}. Wait for it`);
    expect(goodInitdb).not.toHaveBeenCalled();
    expect(fs.readFileSync(lockOf(dataDir), "utf8")).toBe("4242\n");
    expect(fs.readdirSync(path.dirname(dataDir)).sort()).toEqual(["pg", "pg.init.lock"]);
  });

  test("a start whose setup is still running keeps its setup folder", async () => {
    const dataDir = dataDirIn();
    await cutShortSetup(dataDir);
    fs.writeFileSync(lockOf(dataDir), "4242\n");
    await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb, isRunning: () => true }))
      .rejects.toThrow(/Another Celerp start/);
    expect(fs.readFileSync(path.join(`${dataDir}.init`, "postgresql.conf"), "utf8")).toBe("partial");
  });

  test.each([
    ["a process that is no longer running", () => false, 0],
    ["a process ID in use for over ten minutes", () => true, 11 * 60 * 1000],
  ])("a lock left by %s is taken over", async (_label, isRunning, ageMs) => {
    const dataDir = dataDirIn();
    fs.writeFileSync(lockOf(dataDir), "4242\n");
    const now = () => Date.now() + ageMs;
    await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb, isRunning, now })).resolves.toBe(true);
    expect(fs.existsSync(lockOf(dataDir))).toBe(false);
  });

  test("a start that finds the setup finished while it waited leaves the cluster alone", async () => {
    const dataDir = dataDirIn();
    let calls = 0;
    const existsSync = fs.existsSync;
    const finishedMeanwhile = (p) => {
      if (p === path.join(dataDir, "PG_VERSION") && calls++ === 1) fs.writeFileSync(p, "17\n");
      return existsSync(p);
    };
    const spy = jest.spyOn(fs, "existsSync").mockImplementation(finishedMeanwhile);
    try {
      await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb })).resolves.toBe(false);
    } finally {
      spy.mockRestore();
    }
    expect(goodInitdb).not.toHaveBeenCalled();
    expect(fs.existsSync(lockOf(dataDir))).toBe(false);
  });
});

test("a data folder holding files but no cluster is never deleted; the user is told what to do", async () => {
  const dataDir = dataDirIn();
  fs.writeFileSync(path.join(dataDir, "something-of-theirs"), "keep me");
  let message = "";
  await initialiseClusterOnce({ dataDir, runInitdb: goodInitdb }).catch((e) => { message = e.message; });
  expect(message).toContain(dataDir);
  expect(message).toMatch(/move/i);
  expect(message).not.toContain("\u2014");
  expect(goodInitdb).not.toHaveBeenCalled();
  expect(fs.readFileSync(path.join(dataDir, "something-of-theirs"), "utf8")).toBe("keep me");
});

test("a cluster an older version left half set up is kept exactly as it is", async () => {
  // Older versions ran initdb straight into the data folder, so a setup cut short
  // there can leave PG_VERSION without a usable cluster. Starting it then fails
  // with a message (windows-start-errors.js); nothing here may touch it.
  const dataDir = dataDirIn();
  fs.writeFileSync(path.join(dataDir, "PG_VERSION"), "17\n");
  fs.mkdirSync(path.join(dataDir, "base", "1"), { recursive: true });
  fs.writeFileSync(path.join(dataDir, "base", "1", "1259"), "partial");
  await expect(initialiseClusterOnce({ dataDir, runInitdb: goodInitdb })).resolves.toBe(false);
  expect(goodInitdb).not.toHaveBeenCalled();
  expect(fs.readdirSync(dataDir).sort()).toEqual(["PG_VERSION", "base"]);
  expect(fs.readFileSync(path.join(dataDir, "base", "1", "1259"), "utf8")).toBe("partial");
});

// initdb writes the path of its input files into SQL on a UTF-8 database, so a
// per-user install under an account named with accents must hand it an ASCII path.
describe("initdbShareDir", () => {
  const share = "C:\\Users\\Célerp Tëst ü\\AppData\\Local\\Programs\\Celerp\\native\\share";

  test("an ASCII folder is used as it is, without asking Windows", () => {
    const shortPath = jest.fn();
    const plain = "C:\\Program Files\\Celerp\\native\\share";
    expect(initdbShareDir(plain, shortPath)).toBe(plain);
    expect(shortPath).not.toHaveBeenCalled();
  });

  test("a folder with letters outside ASCII is given as its short form", () => {
    const shortPath = jest.fn(() => "C:\\Users\\CLERPT~1\\AppData\\Local\\Programs\\Celerp\\native\\share");
    expect(initdbShareDir(share, shortPath)).toBe("C:\\Users\\CLERPT~1\\AppData\\Local\\Programs\\Celerp\\native\\share");
    expect(shortPath).toHaveBeenCalledWith(share);
  });

  test.each([["no short form", ""], ["a short form that is not ASCII either", share]])(
    "%s stops the setup with a message saying how to reinstall", (_label, result) => {
      expect(() => initdbShareDir(share, () => result))
        .toThrow(/cannot set up its database from .*Reinstall Celerp for all users/);
    });
});
