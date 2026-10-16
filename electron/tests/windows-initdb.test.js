// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// The first database setup on Windows either completes or leaves nothing behind:
// initdb writes into a separate folder that is moved into place only when it
// succeeds, so a setup cut short is simply run again on the next start and a
// folder holding someone's data is never deleted.
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { initialiseClusterOnce } = require("../windows-initdb");

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
  expect(fs.existsSync(`${dataDir}.init`)).toBe(false);
});

test("a setup cut short (app closed or PC switched off) is run again cleanly on the next start", async () => {
  const dataDir = dataDirIn();
  // What an interrupted initdb leaves: a partly written setup folder.
  fs.mkdirSync(path.join(`${dataDir}.init`, "base", "1"), { recursive: true });
  fs.writeFileSync(path.join(`${dataDir}.init`, "postgresql.conf"), "partial");
  await initialiseClusterOnce({ dataDir, runInitdb: goodInitdb });
  expect(goodInitdb).toHaveBeenCalledTimes(1);
  expect(fs.existsSync(path.join(dataDir, "PG_VERSION"))).toBe(true);
  expect(fs.existsSync(path.join(dataDir, "postgresql.conf"))).toBe(false);
  expect(fs.existsSync(`${dataDir}.init`)).toBe(false);
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
