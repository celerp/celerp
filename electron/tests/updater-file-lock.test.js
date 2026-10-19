// Copyright (c) 2026 Noah Severs
// SPDX-License-Identifier: BUSL-1.1
//
// electron-updater moves a finished download from temp-<name> to <name> in its
// pending folder. On Windows, antivirus holds a freshly written installer for a
// while, and the move or delete then fails with EPERM, EACCES or EBUSY. The
// pnpm patch of electron-updater waits that out for up to 60 seconds, and a
// cached installer it cannot remove never fails the update. These tests run the
// installed electron-updater's own download step with the Windows errors faked,
// so they fail if a dependency bump drops the patch.

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");

const updaterDir = path.dirname(require.resolve("electron-updater/package.json"));
const fromUpdater = (id) => require(require.resolve(id, { paths: [updaterDir] }));
const { AppUpdater } = require("electron-updater/out/AppUpdater");
const { DownloadedUpdateHelper } = require("electron-updater/out/DownloadedUpdateHelper");
const fsExtra = fromUpdater("fs-extra");
const { CancellationToken } = fromUpdater("builder-util-runtime");

const NAME = "Celerp-Setup.exe";

function windowsError(code, op) {
  return Object.assign(new Error(`${code}: operation not permitted, ${op}`), { code });
}

// fn(original, args, call) decides each call: throw to fail it, or return
// original(...args) to let it through. `call` counts the calls fn has seen.
function intercept(name, fn) {
  const original = fsExtra[name];
  let call = 0;
  return jest.spyOn(fsExtra, name).mockImplementation((...args) => fn(original, args, call++));
}

let cacheDir;
let warnings;

beforeEach(() => {
  cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "celerp-updater-"));
  warnings = [];
  jest.useFakeTimers({ doNotFake: ["setImmediate", "nextTick", "queueMicrotask"] });
});

afterEach(() => {
  jest.restoreAllMocks();
  jest.useRealTimers();
  fs.rmSync(cacheDir, { recursive: true, force: true });
});

const pending = () => path.join(cacheDir, "pending");
const finalFile = () => path.join(pending(), NAME);
const tempFile = () => path.join(pending(), `temp-${NAME}`);

// Run electron-updater's download step for a new installer, with the download
// itself writing "new" to the temp file. Fake time runs on until it settles.
// Returns { result, error, downloadedFile, seconds }.
async function download() {
  const helper = new DownloadedUpdateHelper(cacheDir);
  const log = { info: () => {}, warn: (m) => warnings.push(String(m)), error: () => {}, debug: () => {} };
  const self = { _logger: log, listenerCount: () => 0, emit: () => {}, getOrCreateDownloadHelper: async () => helper };
  let downloadedFile = null;
  const taskOptions = {
    fileExtension: "exe",
    fileInfo: { url: new URL(`https://example.invalid/${NAME}`), info: { url: NAME, sha512: "new-sha" }, packageInfo: null },
    downloadUpdateOptions: {
      requestHeaders: {},
      cancellationToken: new CancellationToken(),
      updateInfoAndProvider: { info: { version: "2.6.1" } },
    },
    task: async (dest) => fs.promises.writeFile(dest, "new"),
    done: async (info) => { downloadedFile = info.downloadedFile; },
  };
  const started = Date.now();
  let settled = false;
  let result;
  let error;
  AppUpdater.prototype.executeDownload.call(self, taskOptions)
    .then((r) => { result = r; }, (e) => { error = e; })
    .finally(() => { settled = true; });
  for (let i = 0; i < 400 && !settled; i++) {
    await new Promise((resolve) => setImmediate(resolve));
    await jest.advanceTimersByTimeAsync(250);
  }
  if (!settled) throw new Error("download never settled");
  return { result, error, downloadedFile, seconds: (Date.now() - started) / 1000 };
}

test("a download antivirus holds for a few seconds (EPERM) is saved once it is released",
  async function test_transient_eperm_on_rename_is_retried() {
    const rename = intercept("rename", (original, args, call) => {
      if (call < 10) throw windowsError("EPERM", "rename");
      return original(...args);
    });
    const { error, downloadedFile } = await download();
    expect(error).toBeUndefined();
    expect(rename).toHaveBeenCalledTimes(11);
    expect(downloadedFile).toBe(finalFile());
    expect(fs.readFileSync(finalFile(), "utf8")).toBe("new");
  });

test("EACCES while the download is held is waited out the same way",
  async function test_transient_eacces_on_rename_is_retried() {
    intercept("rename", (original, args, call) => {
      if (call < 4) throw windowsError("EACCES", "rename");
      return original(...args);
    });
    const { error, downloadedFile } = await download();
    expect(error).toBeUndefined();
    expect(downloadedFile).toBe(finalFile());
  });

test("a download that stays locked fails after about 60 seconds of retries, not at once",
  async function test_permanent_lock_fails_after_sixty_seconds() {
    const rename = intercept("rename", () => { throw windowsError("EPERM", "rename"); });
    const { error, seconds } = await download();
    expect(error && error.message).toMatch(/^EPERM:/);
    expect(rename.mock.calls.length).toBeGreaterThan(100);
    expect(seconds).toBeGreaterThanOrEqual(59);
    expect(seconds).toBeLessThanOrEqual(62);
  });

// Hold `file` the way antivirus does on Windows, for `seconds` of (fake) time,
// or for good when seconds is Infinity: deleting it, or renaming onto or from
// it, fails with EPERM.
function holdFile(file, seconds) {
  const until = Date.now() + seconds * 1000;
  const held = (p) => p === file && Date.now() < until;
  intercept("unlink", (original, args) => {
    if (held(args[0])) throw windowsError("EPERM", "unlink");
    return original(...args);
  });
  intercept("rename", (original, args) => {
    if (held(args[0]) || held(args[1])) throw windowsError("EPERM", "rename");
    return original(...args);
  });
}

function cacheOldInstaller() {
  fs.mkdirSync(pending(), { recursive: true });
  fs.writeFileSync(finalFile(), "old");
}

test("an old cached installer held for a few seconds is removed, and the new one takes its name",
  async function test_transient_lock_on_old_installer_is_retried() {
    cacheOldInstaller();
    holdFile(finalFile(), 5);
    const { error, downloadedFile } = await download();
    expect(error).toBeUndefined();
    expect(downloadedFile).toBe(finalFile());
    expect(fs.readFileSync(finalFile(), "utf8")).toBe("new");
  });

test("an old cached installer that cannot be removed is logged, and the update goes ahead without it",
  async function test_permanently_locked_old_installer_does_not_fail_the_update() {
    cacheOldInstaller();
    holdFile(finalFile(), Infinity);
    const { error, downloadedFile } = await download();
    expect(error).toBeUndefined();
    expect(downloadedFile).not.toBe(finalFile());
    expect(fs.readFileSync(downloadedFile, "utf8")).toBe("new");
    expect(warnings.join("\n")).toMatch(/old cached installer/i);
    // The update-info.json the next start reads names the file actually saved.
    const info = JSON.parse(fs.readFileSync(path.join(pending(), "update-info.json"), "utf8"));
    expect(info.fileName).toBe(path.basename(downloadedFile));
  });

test("electron-updater is patched for exactly the installed version",
  function test_patch_targets_installed_electron_updater() {
    const pkg = require("../package.json");
    const { version } = require("electron-updater/package.json");
    expect(pkg.pnpm.patchedDependencies[`electron-updater@${version}`]).toBe(`patches/electron-updater@${version}.patch`);
  });
