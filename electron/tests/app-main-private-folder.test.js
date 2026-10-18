// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// Windows: when the user's private database folder under ProgramData cannot be
// used, the message says what to do with it. A folder of the user's own that other
// accounts can open may hold the user's database, so it is never to be removed.
"use strict";

const childProcessReal = require("child_process");
const { loadAppMain } = require("./app-main-loader.lib");

const FOLDER = "C:\\ProgramData\\Celerp-S-1-5-21-9";

function refusedWith(problem) {
  const spawnSync = jest.fn(() => ({ status: 1, stdout: "", stderr: `win_restricted_launch: ${FOLDER} ${problem}\n` }));
  const { windowsPrivateRoot } = loadAppMain({
    platform: "win32",
    extraFakes: { child_process: { ...childProcessReal, spawnSync } },
  });
  const programData = process.env.ProgramData;
  process.env.ProgramData = "C:\\ProgramData";
  try {
    windowsPrivateRoot();
  } catch (e) {
    return e.message;
  } finally {
    if (programData === undefined) delete process.env.ProgramData;
    else process.env.ProgramData = programData;
  }
  throw new Error("the folder was not refused");
}

test("a folder of the user's own that other accounts can open is kept, never to be removed", () => {
  const message = refusedWith("can be opened by other accounts");
  expect(message).toContain(`${FOLDER} can be opened by other accounts`);
  expect(message).toContain("do not delete it");
  expect(message).not.toContain("remove that folder");
  expect(message).not.toContain("Your data is not affected");
});

test("a folder another account made in its place is to be removed", () => {
  const message = refusedWith("belongs to another account (S-1-5-21-8)");
  expect(message).toContain("remove that folder");
  expect(message).toContain("Your data is not affected");
});
